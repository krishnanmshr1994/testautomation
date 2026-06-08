from stagehand import Stagehand
from src.planner import TestPlan
from pydantic import BaseModel

class VerificationResult(BaseModel):
    success: bool
    details: str

async def execute_plan(stagehand: Stagehand, plan: TestPlan) -> list:
    """
    Iterates through the test plan, acting on the page and verifying outcomes.
    """
    results = []
    
    for idx, intent in enumerate(plan.intents):
        print(f"\n--- Executing Step {idx + 1}/{len(plan.intents)} ---")
        print(f"Action: {intent.description}")
        
        step_result = {
            "intent": intent.model_dump(),
            "action_success": False,
            "verification_success": False,
            "error": None,
            "details": ""
        }
        
        try:
            # 1. Perform the action
            action_result = await stagehand.page.act({"action": intent.description})
            step_result["action_success"] = action_result.success
            
            if not action_result.success:
                print(f"Action failed: {action_result.message}")
                step_result["error"] = action_result.message
                results.append(step_result)
                continue
                
            # 2. Verify the state
            print(f"Verifying: {intent.expected_outcome}")
            verification = await stagehand.page.extract({
                "instruction": f"Verify if the following outcome occurred: {intent.expected_outcome}",
                "schema": VerificationResult
            })
            
            step_result["verification_success"] = verification.success
            step_result["details"] = verification.details
            print(f"Verification {'Passed' if verification.success else 'Failed'}: {verification.details}")
            
        except Exception as e:
            print(f"Error during execution: {str(e)}")
            step_result["error"] = str(e)
            
        results.append(step_result)
        
    return results
