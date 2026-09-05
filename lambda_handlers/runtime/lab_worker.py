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
    asyncio.run(lab.process(event.get("job_id", ""), event.get("operation", "")))
    return {"processed": True}
