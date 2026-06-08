import os
import json
from pydantic import BaseModel, Field
from typing import List, Optional
from stagehand import Stagehand

class TestIntent(BaseModel):
    description: str = Field(..., description="A natural language description of the action to perform (e.g., 'Click the login button')")
    expected_outcome: str = Field(..., description="The expected outcome of the action to verify (e.g., 'A success message appears')")
    is_security_probe: bool = Field(False, description="Whether this action is injecting a security payload like XSS or SQLi")

class TestPlan(BaseModel):
    intents: List[TestIntent]

async def generate_test_plan(stagehand: Stagehand) -> TestPlan:
    """
    Observes the current page to map elements and uses an LLM to generate a test plan.
    """
    print("Observing page elements...")
    observations = await stagehand.page.observe({
        "instruction": "Identify all interactive elements, forms, inputs, and buttons."
    })
    
    # We will pass these observations to the LLM to generate a plan.
    # For now, we simulate the LLM call using Stagehand's extract or standard OpenAI.
    # We use stagehand.page.extract to force an LLM extraction of a test plan based on the DOM.
    print("Generating test plan based on observations...")
    
    test_plan_prompt = (
        "Based on the elements on this page, generate a QA and Security test plan. "
        "Include happy paths, negative paths, and security probes (like XSS payload '<script>alert(1)</script>' "
        "or SQLi '' OR 1=1')."
    )
    
    # Using Stagehand's extract capability to directly extract structured data (TestPlan)
    plan = await stagehand.page.extract({
        "instruction": test_plan_prompt,
        "schema": TestPlan
    })
    
    print(f"Generated {len(plan.intents)} test intents.")
    return plan
