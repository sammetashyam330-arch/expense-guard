import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ExpenseGuard MCP Server")

@mcp.tool()
def get_department_budget(department: str) -> str:
    """Get the remaining budget for a department.
    
    Args:
        department: Name of the department (e.g. Sales, Engineering, Marketing, HR)
    """
    budgets = {
        "sales": "$15,000 remaining",
        "engineering": "$45,000 remaining",
        "marketing": "$5,000 remaining",
        "hr": "$8,000 remaining"
    }
    return budgets.get(department.lower(), "Department not found. Default budget remaining: $2,000")

@mcp.tool()
def get_employee_history(employee_name: str) -> str:
    """Get the historical expense report status for an employee.
    
    Args:
        employee_name: Full name of the employee
    """
    # Simple mocked database
    if "viola" in employee_name.lower():
        return f"Employee {employee_name} has submitted 8 expenses. 4 APPROVED, 4 REJECTED due to policy violations (alcohol/non-itemized receipts)."
    return f"Employee {employee_name} has submitted 5 expenses in the last 90 days. 4 APPROVED, 1 REJECTED (missing receipt)."

@mcp.tool()
def lookup_policy_rule(category: str) -> str:
    """Look up policy rules for a specific expense category.
    
    Args:
        category: Expense category (e.g. Meals, Travel, Software, Office Supplies)
    """
    rules = {
        "meals": "Meals: Limit of $100 per day per person. Must list client/project names in description. No alcohol without prior approval.",
        "travel": "Travel: Economy class only for flights under 6 hours. Lodging limit is $250/night. Receipts required for all transactions.",
        "software": "Software: Subscription licenses require pre-approval by IT/Manager. Must specify the business purpose.",
        "office supplies": "Office Supplies: Items over $200 require manager approval. Use preferred vendor list when possible."
    }
    return rules.get(category.lower(), "General policy: Receipts required for all expenses over $25. Must have clear business purpose.")

if __name__ == "__main__":
    mcp.run()
