"""Asynchronous dev Nutrition Lab adapter; never sends Telegram messages."""
import asyncio
import os

from macro_bot.nutrition_lab import NutritionLab, enabled
from macro_bot.serverless_data import DynamoNutritionRepository
from macro_bot.serverless_service import NutritionService


def handler(event, context):
    if not enabled():
        return {"processed": False}
    import boto3
    table_name = os.environ["FITNESS_DATA_TABLE"]
    service = NutritionService(DynamoNutritionRepository(
        boto3.resource("dynamodb").Table(table_name), table_name=table_name, client=boto3.client("dynamodb"),
    ))
    lab = NutritionLab(service)
    if event == {"operation": "recommendation_scenarios"}:
        # IAM-invoked dev smoke only. No HTTP route, clock input or Telegram destination.
        return asyncio.run(lab.recommendation_scenarios())
    if set(event) == {"operation", "scenario"} and event.get("operation") == "retrospective_scenario":
        from macro_bot.recommendation_scenarios import run_scenario
        return asyncio.run(run_scenario(service, event["scenario"]))
    asyncio.run(lab.process(event.get("job_id", ""), event.get("operation", "")))
    return {"processed": True}
