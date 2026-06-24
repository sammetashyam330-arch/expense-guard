import sys
import re
import json
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from google.adk.workflow import Workflow, node, START
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.genai import types

from app.config import config

# Define input schema for the workflow
class ExpenseReportInput(BaseModel):
    employee_name: str = Field(description="Name of the employee submitting the expense")
    amount: float = Field(description="The total expense amount in USD")
    category: str = Field(description="Category of the expense (e.g. Meals, Travel, Software, Office Supplies)")
    description: str = Field(description="A detailed description of the expense item(s)")

# MCP Toolset connection parameters to run our local mcp_server.py
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server"],
        )
    )
)

# Specialist Agent 1: Policy Auditor (uses MCP toolset)
policy_auditor = LlmAgent(
    name="policy_auditor",
    model=config.model,
    instruction=(
        "You are the Expense Policy Auditor. Your task is to verify if the expense complies with company policies.\n"
        "Use the lookup_policy_rule tool to check the rules for the expense category.\n"
        "Also check the employee's history using get_employee_history to see if they are a repeat offender.\n"
        "Formulate a brief audit response outlining any violations or confirming compliance."
    ),
    tools=[mcp_toolset],
    description="Audits expense reports against company policy database and checks employee history.",
)

# Specialist Agent 2: Anomaly Detector (uses MCP toolset)
anomaly_detector = LlmAgent(
    name="anomaly_detector",
    model=config.model,
    instruction=(
        "You are the Expense Anomaly Detector. Your task is to check if the expense amount is an anomaly\n"
        "or if it exceeds the remaining budget.\n"
        "Use the get_department_budget tool to check the department's remaining budget.\n"
        "Check if the amount is reasonable for the given category.\n"
        "Formulate a brief response stating whether this expense is anomalous or exceeds budget."
    ),
    tools=[mcp_toolset],
    description="Detects anomalous expense patterns and checks department budget limits.",
)

# Orchestrator Agent (coordinates specialists via AgentTools)
orchestrator = LlmAgent(
    name="orchestrator",
    model=config.model,
    instruction=(
        "You are the ExpenseGuard Orchestrator.\n"
        "You have received a scrubbed expense report in state: {scrubbed_input}.\n"
        "Coordinate with your specialists:\n"
        "1. Call the policy_auditor agent tool to audit policy compliance.\n"
        "2. Call the anomaly_detector agent tool to check for anomalies and budget issues.\n"
        "Then, write a summary and determine the final outcome.\n"
        "Your final response MUST end with a line in the format:\n"
        "DECISION: <APPROVED / REJECTED / NEEDS_REVIEW>."
    ),
    tools=[AgentTool(policy_auditor), AgentTool(anomaly_detector)],
)

# Workflow node: Security Checkpoint
def security_checkpoint(ctx: Context, node_input: ExpenseReportInput) -> Event:
    input_data = node_input.model_dump()
    
    # 1. PII scrubbing: check and scrub Credit Cards / SSNs
    cc_regex = r"\b(?:\d[ -]*?){13,16}\b"
    ssn_regex = r"\b\d{3}-\d{2}-\d{4}\b"
    
    scrubbed_desc = re.sub(cc_regex, "[REDACTED_CC]", input_data["description"])
    scrubbed_desc = re.sub(ssn_regex, "[REDACTED_SSN]", scrubbed_desc)
    
    scrubbed_name = re.sub(cc_regex, "[REDACTED_CC]", input_data["employee_name"])
    scrubbed_name = re.sub(ssn_regex, "[REDACTED_SSN]", scrubbed_name)
    
    input_data["description"] = scrubbed_desc
    input_data["employee_name"] = scrubbed_name
    
    # 2. Prompt injection keyword detection
    injection_keywords = ["ignore previous", "system instructions", "system prompt", "override", "bypass rules"]
    is_injection = False
    for kw in injection_keywords:
        if kw in scrubbed_desc.lower() or kw in scrubbed_name.lower():
            is_injection = True
            break
            
    if is_injection:
        audit_log = {
            "event": "security_violation",
            "reason": "Potential prompt injection keywords detected",
            "severity": "CRITICAL",
            "employee": scrubbed_name
        }
        print(f"AUDIT_LOG: {json.dumps(audit_log)}")
        return Event(
            output="Security Violation: Potential prompt injection attempt blocked.",
            route="SECURITY_EVENT",
            state={"security_status": "BLOCKED"}
        )
        
    # 3. Domain-specific rule: Single expense limit ($10,000 max) or non-positive amount
    if input_data["amount"] <= 0:
        audit_log = {
            "event": "policy_violation",
            "reason": "Non-positive expense amount",
            "severity": "WARNING",
            "employee": scrubbed_name
        }
        print(f"AUDIT_LOG: {json.dumps(audit_log)}")
        return Event(
            output="Policy Violation: Expense amount must be greater than zero.",
            route="SECURITY_EVENT",
            state={"security_status": "BLOCKED"}
        )
    elif input_data["amount"] > 10000:
        audit_log = {
            "event": "policy_violation",
            "reason": "Expense amount exceeds $10,000 threshold",
            "severity": "WARNING",
            "employee": scrubbed_name
        }
        print(f"AUDIT_LOG: {json.dumps(audit_log)}")
        return Event(
            output="Policy Violation: Individual expenses exceeding $10,000 are not permitted.",
            route="SECURITY_EVENT",
            state={"security_status": "BLOCKED"}
        )
        
    audit_log = {
        "event": "security_pass",
        "severity": "INFO",
        "employee": scrubbed_name,
        "amount": input_data["amount"]
    }
    print(f"AUDIT_LOG: {json.dumps(audit_log)}")
    
    # Store the scrubbed input in ctx.state for other nodes
    return Event(
        output=input_data,
        route="__DEFAULT__",
        state={"scrubbed_input": input_data}
    )

# Workflow node: Security Block Handler
def security_block(node_input: str):
    message = f"ExpenseGuard Blocked Action:\n\n{node_input}"
    yield Event(
        content=types.Content(
            role='model',
            parts=[types.Part.from_text(text=message)]
        )
    )
    yield Event(output=message)

# Workflow node: Review Router (decides routing based on orchestrator decision)
def review_router(ctx: Context, node_input: types.Content) -> Event:
    text = ""
    if node_input and node_input.parts:
        text = "".join([p.text for p in node_input.parts if p.text])
        
    ctx.state["orchestrator_summary"] = text
    
    # Determine the decision using text analysis
    decision = "APPROVED"
    severity = "INFO"
    if "NEEDS_REVIEW" in text.upper() or "REVIEW" in text.upper():
        decision = "NEEDS_REVIEW"
        severity = "WARNING"
    elif "REJECTED" in text.upper():
        decision = "REJECTED"
        severity = "WARNING"
        
    audit_log = {
        "event": "orchestrator_decision",
        "decision": decision,
        "severity": severity,
        "reason_summary": text[:200]
    }
    print(f"AUDIT_LOG: {json.dumps(audit_log)}")
    
    if decision == "NEEDS_REVIEW":
        return Event(output=text, route="NEEDS_REVIEW")
    elif decision == "REJECTED":
        return Event(output=text, route="REJECTED")
    else:
        return Event(output=text, route="APPROVED")

# Workflow node: Human-in-the-Loop review
@node(name="human_review", rerun_on_resume=True)
async def human_review(ctx: Context, node_input: str):
    if not ctx.resume_inputs or "manager_approval" not in ctx.resume_inputs:
        yield RequestInput(
            interrupt_id="manager_approval",
            message=f"Expense needs manual manager review. Summary:\n{node_input}\n\nApprove or Reject? (Type 'Approve' or 'Reject')"
        )
        return
        
    response = ctx.resume_inputs["manager_approval"].strip().upper()
    
    audit_log = {
        "event": "hitl_response",
        "response": response,
        "severity": "INFO"
    }
    print(f"AUDIT_LOG: {json.dumps(audit_log)}")
    
    if "APPROVE" in response:
        yield Event(
            output="Manager Approved: Expense report has been approved after manual review.",
            state={"hitl_decision": "APPROVED"}
        )
    else:
        yield Event(
            output="Manager Rejected: Expense report has been rejected after manual review.",
            state={"hitl_decision": "REJECTED"}
        )

# Workflow node: Final Output formatter
def final_output(ctx: Context, node_input: str):
    message = f"ExpenseGuard Final Result:\n\n{node_input}"
    yield Event(
        content=types.Content(
            role='model',
            parts=[types.Part.from_text(text=message)]
        )
    )
    yield Event(output=message)

# Construct the Graph Workflow
root_agent = Workflow(
    name="expense_guard",
    edges=[
        (START, security_checkpoint),
        (security_checkpoint, security_block, "SECURITY_EVENT"),
        (security_checkpoint, orchestrator, "__DEFAULT__"),
        (orchestrator, review_router),
        (review_router, human_review, "NEEDS_REVIEW"),
        (review_router, final_output, "__DEFAULT__"),
        (security_block, final_output),
        (human_review, final_output),
    ],
    input_schema=ExpenseReportInput,
    description="Orchestrator for business expense validation, policy compliance and anomaly check.",
)

# Instantiate the App
app = App(
    name="app",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True)
)
