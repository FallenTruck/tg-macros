"""Shared DynamoDB serialization, consistent gets, queries and transactions."""
from typing import Any, Mapping, Optional, Sequence
from decimal import Decimal
from boto3.dynamodb.types import TypeSerializer

def to_storage(value: Any) -> Any:
    """Convert JSON-like values to values accepted by the DynamoDB resource."""

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): to_storage(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_storage(item) for item in value]
    return value


def from_storage(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value) if value % 1 else int(value)
    if isinstance(value, Mapping):
        return {str(key): from_storage(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_storage(item) for item in value]
    return value


def is_conditional_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        code = response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return True
    return type(error).__name__ in {
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
    }


class DynamoDBStore:
    def __init__(self, table: Any, table_name: str, client: Any = None):
        self.table = table
        self.table_name = table_name
        self.client = client

    def query_page(self, **kwargs: Any) -> dict[str, Any]:
        return self.table.query(**kwargs)

    def put_item(self, item: Mapping[str, Any], **kwargs: Any) -> None:
        self.table.put_item(Item=to_storage(item), **kwargs)

    def get_item(self, key: Mapping[str, str]) -> Optional[dict[str, Any]]:
        try:
            result = self.table.get_item(Key=dict(key), ConsistentRead=True)
        except TypeError:
            result = self.table.get_item(Key=dict(key))
        item = result.get("Item") if isinstance(result, Mapping) else None
        return dict(item) if item else None

    def query(self, key_expression: Any, **kwargs: Any) -> list[dict[str, Any]]:
        query_kwargs = dict(kwargs)
        results: list[dict[str, Any]] = []
        requested_limit = query_kwargs.get("Limit")
        while True:
            result = self.table.query(KeyConditionExpression=key_expression, **query_kwargs)
            if isinstance(result, Mapping):
                results.extend(dict(item) for item in result.get("Items", []))
                last_key = result.get("LastEvaluatedKey")
            else:
                last_key = None
            if requested_limit is not None and len(results) >= int(requested_limit):
                return results[: int(requested_limit)]
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
            if requested_limit is not None:
                query_kwargs["Limit"] = max(1, int(requested_limit) - len(results))
        return results

    def transact_write(self, operations: Sequence[dict[str, Any]]) -> None:
        low_level_operations: list[dict[str, Any]] = []
        serializer = TypeSerializer()
        for operation in operations:
            operation_name = str(operation.get("operation", "Put"))
            # TableName belongs on every low-level transaction operation, not
            # on the TransactWriteItems request itself.
            converted = {key: value for key, value in operation.items() if key != "operation"}
            if "Item" in converted:
                converted["Item"] = {
                    key: serializer.serialize(to_storage(value))
                    for key, value in converted["Item"].items()
                }
            if "Key" in converted:
                converted["Key"] = {
                    key: serializer.serialize(to_storage(value))
                    for key, value in converted["Key"].items()
                }
            if "ExpressionAttributeValues" in converted:
                converted["ExpressionAttributeValues"] = {
                    key: serializer.serialize(to_storage(value))
                    for key, value in converted["ExpressionAttributeValues"].items()
                }
            low_level_operations.append({operation_name: converted})

        if self.client is not None and hasattr(self.client, "transact_write_items"):
            self.client.transact_write_items(TransactItems=low_level_operations)
            return
        if hasattr(self.table, "transact_write_items"):
            self.table.transact_write_items(TransactItems=low_level_operations)
            return
        raise RuntimeError("DynamoDB transaction client is not configured")
