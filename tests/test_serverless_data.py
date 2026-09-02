import copy
import unittest
from datetime import datetime, timezone

from boto3.dynamodb.types import TypeDeserializer

from macro_bot.models import MealEstimate, MealItemEstimate, MacroTotal, QuestionnaireAnswers, UserProfile
from macro_bot.serverless_data import (
    ActionExpired,
    ActionNotFound,
    DynamoNutritionRepository,
    local_day_utc_bounds,
)


class ConditionalCheckFailedException(Exception):
    pass


class TransactionCanceledException(Exception):
    pass


class _Exceptions:
    ConditionalCheckFailedException = ConditionalCheckFailedException
    TransactionCanceledException = TransactionCanceledException


def _condition_matches(expression, item, names=None, values=None):
    if not expression:
        return True
    names = names or {}
    values = values or {}
    expression = expression.replace("#status", names.get("#status", "status"))
    expression = expression.replace("#state", names.get("#state", "state"))
    if "attribute_not_exists(PK)" in expression:
        return item is None
    if item is None:
        return False
    if "status = :processing" in expression:
        return item.get("status") == values.get(":processing")
    if "status = :pending" in expression:
        return item.get("status") == values.get(":pending") and (
            "expires_at" not in expression or item.get("expires_at", 0) > values.get(":now", 0)
        )
    if "state = :awaiting" in expression:
        return item.get("state") == values.get(":awaiting") and item.get("expires_at", 0) > values.get(":now", 0)
    if "state = :selected" in expression:
        return item.get("state") == values.get(":selected") and item.get("expires_at", 0) > values.get(":now", 0)
    return True


def _expression_values(raw):
    return {key: TypeDeserializer().deserialize(value) for key, value in raw.items()}


class _FakeClient:
    def __init__(self, table):
        self.table = table

    def transact_write_items(self, **kwargs):
        if "TableName" in kwargs:
            raise AssertionError("transaction request must not include top-level TableName")
        operations = kwargs["TransactItems"]
        pending = copy.deepcopy(self.table.items)
        deserializer = TypeDeserializer()
        for wrapper in operations:
            operation, payload = next(iter(wrapper.items()))
            if payload.get("TableName") != self.table.name:
                raise AssertionError("transaction operation must include TableName")
            key = payload.get("Key") or payload.get("Item")
            key = {name: deserializer.deserialize(value) for name, value in key.items()}
            item_key = (key["PK"], key["SK"])
            item = pending.get(item_key)
            values = _expression_values(payload.get("ExpressionAttributeValues", {}))
            if not _condition_matches(
                payload.get("ConditionExpression"),
                item,
                payload.get("ExpressionAttributeNames"),
                values,
            ):
                raise TransactionCanceledException()
            if operation == "Put":
                pending[item_key] = {
                    name: deserializer.deserialize(value)
                    for name, value in payload["Item"].items()
                }
            elif operation == "Update":
                item = copy.deepcopy(item or {})
                names = payload.get("ExpressionAttributeNames", {})
                expression = payload["UpdateExpression"]
                set_expression = expression.split(" SET ", 1)[-1] if " SET " in expression else expression.removeprefix("SET ").strip()
                for assignment in set_expression.split(","):
                    name, value_name = [part.strip() for part in assignment.split("=", 1)]
                    item[names.get(name, name)] = values[value_name]
                pending[item_key] = item
        self.table.items = pending


class _FakeTable:
    name = "fitness"

    def __init__(self):
        self.items = {}
        self.meta = type("Meta", (), {"client": _FakeClient(self)})()

    @staticmethod
    def _key(key):
        return key["PK"], key["SK"]

    def get_item(self, **kwargs):
        item = self.items.get(self._key(kwargs["Key"]))
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, **kwargs):
        key = self._key(kwargs["Item"])
        existing = self.items.get(key)
        if not _condition_matches(kwargs.get("ConditionExpression"), existing):
            raise ConditionalCheckFailedException()
        self.items[key] = copy.deepcopy(kwargs["Item"])

    def update_item(self, **kwargs):
        key = self._key(kwargs["Key"])
        existing = self.items.get(key)
        values = kwargs.get("ExpressionAttributeValues", {})
        if not _condition_matches(
            kwargs.get("ConditionExpression"),
            existing,
            kwargs.get("ExpressionAttributeNames"),
            values,
        ):
            raise ConditionalCheckFailedException()
        old = copy.deepcopy(existing or {})
        item = copy.deepcopy(existing or {})
        names = kwargs.get("ExpressionAttributeNames", {})
        expression = kwargs["UpdateExpression"]
        if expression.startswith("REMOVE"):
            remove_part, set_part = expression.split(" SET ", 1)
            for field in remove_part.replace("REMOVE", "").strip().split(","):
                item.pop(names.get(field.strip(), field.strip()), None)
            expression = "SET " + set_part
        if expression.startswith("SET"):
            for assignment in expression[3:].strip().split(","):
                name, value_name = [part.strip() for part in assignment.split("=", 1)]
                item[names.get(name, name)] = copy.deepcopy(values[value_name])
        self.items[key] = item
        if kwargs.get("ReturnValues") == "ALL_OLD":
            return {"Attributes": old}
        return {}

    def query(self, **kwargs):
        expression = kwargs["KeyConditionExpression"]

        def predicates(node):
            data = node.get_expression()
            if data["operator"] == "AND":
                return predicates(data["values"][0]) + predicates(data["values"][1])
            values = data["values"]
            name = values[0].name
            if data["operator"] == "=":
                return [(name, "eq", values[1])]
            if data["operator"] == "begins_with":
                return [(name, "begins", values[1])]
            if data["operator"] == "BETWEEN":
                return [(name, "between", values[1], values[2])]
            raise AssertionError(data)

        checks = predicates(expression)
        filter_expression = kwargs.get("FilterExpression")
        filter_check = None
        if filter_expression is not None:
            filter_values = filter_expression.get_expression()["values"]
            filter_check = (filter_values[0].__dict__["name"], filter_values[1])
        items = []
        for item in self.items.values():
            if filter_check and item.get(filter_check[0]) != filter_check[1]:
                continue
            if all(
                (item.get(name) == values[0])
                if mode == "eq"
                else (str(item.get(name, "")).startswith(values[0]))
                if mode == "begins"
                else (values[0] <= item.get(name, "") <= values[1])
                for name, mode, *values in checks
            ):
                items.append(copy.deepcopy(item))
        items.sort(key=lambda item: (item["PK"], item["SK"]), reverse=not kwargs.get("ScanIndexForward", True))
        if kwargs.get("Limit"):
            items = items[: kwargs["Limit"]]
        return {"Items": items}


class _PaginatingTable(_FakeTable):
    def query(self, **kwargs):
        result = super().query(**kwargs)
        if kwargs.get("ExclusiveStartKey"):
            return {"Items": getattr(self, "_remaining_page", [])}
        if len(result["Items"]) <= 1:
            return result
        first, rest = result["Items"][0], result["Items"][1:]
        self._remaining_page = rest
        return {
            "Items": [first],
            "LastEvaluatedKey": {"PK": first["PK"], "SK": first["SK"]},
        }


class _RacingIdentityTable(_FakeTable):
    def __init__(self):
        super().__init__()
        self.race_injected = False

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        if item.get("PK", "").startswith("IDENTITY#") and not self.race_injected:
            self.race_injected = True
            super().put_item(**kwargs)
            raise ConditionalCheckFailedException()
        return super().put_item(**kwargs)


def _estimate(calories=500):
    return MealEstimate(
        meal_name="rice bowl",
        calories=calories,
        protein_g=30,
        carbs_g=60,
        fat_g=10,
        confidence=0.8,
        notes="standard portion",
        items=[MealItemEstimate("rice", 250, "one bowl", calories, 30, 60, 10)],
        total_low=MacroTotal(calories - 50, 25, 50, 8),
        total_high=MacroTotal(calories + 100, 38, 75, 15),
        variance_drivers=["portion size"],
    )


class ServerlessDataTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
        self.table = _FakeTable()
        self.repo = DynamoNutritionRepository(self.table, table_name="fitness", now_fn=lambda: self.now)

    def test_identity_creation_is_idempotent_and_metadata_is_mutable(self):
        first = self.repo.resolve_identity(101, "old_name", "Old Name")
        second = self.repo.resolve_identity(101, "new_name", "New Name")
        other = self.repo.resolve_identity(202, "other", "Other")
        self.assertEqual(first.user_id, second.user_id)
        self.assertNotEqual(first.user_id, other.user_id)
        identity_item = self.table.items[("IDENTITY#TELEGRAM#101", "USER")]
        self.assertEqual(identity_item["username"], "new_name")
        self.assertEqual(identity_item["user_id"], first.user_id)

    def test_identity_creation_handles_conditional_race_without_creating_a_second_user(self):
        table = _RacingIdentityTable()
        repo = DynamoNutritionRepository(table, table_name="fitness")
        first = repo.resolve_identity(303, "first", "First")
        second = repo.resolve_identity(303, "second", "Second")
        self.assertEqual(first.user_id, second.user_id)
        self.assertEqual(len([key for key in table.items if key[0].startswith("IDENTITY#")]), 1)

    def test_profile_target_history_and_effective_lookup(self):
        identity = self.repo.resolve_identity(101, "u", "User")
        answers = QuestionnaireAnswers("male", 30, 180, 80, "moderate", "maintain")
        old = UserProfile(101, "u", "User", MacroTotal(2000, 150, 200, 60), answers, created_at="2026-01-01T00:00:00Z")
        new = UserProfile(101, "u", "User", MacroTotal(2300, 160, 250, 70), answers, created_at="2026-01-01T00:00:00Z")
        self.repo.save_profile(identity, old, effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.repo.save_profile(identity, new, effective_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(len(self.repo.list_targets(identity.user_id)), 2)
        self.assertEqual(self.repo.target_effective_at(identity.user_id, datetime(2026, 1, 15, tzinfo=timezone.utc)).calories, 2000)
        self.assertEqual(self.repo.target_effective_at(identity.user_id, datetime(2026, 2, 2, tzinfo=timezone.utc)).calories, 2300)

    def test_effective_lookup_before_first_target_does_not_use_current_target(self):
        identity = self.repo.resolve_identity(101, "u", "User")
        answers = QuestionnaireAnswers("male", 30, 180, 80, "moderate", "maintain")
        profile = UserProfile(101, "u", "User", MacroTotal(2000, 150, 200, 60), answers)
        self.repo.save_profile(identity, profile, effective_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
        self.assertIsNone(self.repo.target_effective_at(identity.user_id, datetime(2026, 1, 1, tzinfo=timezone.utc)))

    def test_query_paginates_all_user_partition_items(self):
        table = _PaginatingTable()
        repo = DynamoNutritionRepository(table, table_name="fitness", now_fn=lambda: self.now)
        identity = repo.resolve_identity(101, "u", "User")
        answers = QuestionnaireAnswers("male", 30, 180, 80, "moderate", "maintain")
        repo.save_profile(identity, UserProfile(101, "u", "User", MacroTotal(2000, 150, 200, 60), answers))
        repo.save_profile(identity, UserProfile(101, "u", "User", MacroTotal(2100, 150, 200, 60), answers))
        self.assertEqual(len(repo.list_targets(identity.user_id)), 2)

    def test_query_limit_is_preserved_across_pages_for_recent_meals(self):
        table = _PaginatingTable()
        repo = DynamoNutritionRepository(table, table_name="fitness", now_fn=lambda: self.now)
        identity = repo.resolve_identity(101, "u", "User")
        for index in range(3):
            action = repo.create_pending_meal(
                identity,
                chat_id=9,
                request_message_id=20 + index,
                caption=f"rice bowl {index}",
                estimate=_estimate(),
                eaten_at=datetime(2026, 1, 15, 4 + index, tzinfo=timezone.utc),
            )
            repo.finalize_action(identity, action.token, "confirm")
        self.assertEqual(len(repo.list_recent_meals(identity, limit=2)), 2)

    def test_recent_meals_limit_counts_confirmed_records_after_cancelled_record(self):
        table = _FakeTable()
        repo = DynamoNutritionRepository(table, table_name="fitness", now_fn=lambda: self.now)
        identity = repo.resolve_identity(101, "u", "User")
        cancelled = repo.create_pending_meal(
            identity,
            chat_id=9,
            request_message_id=30,
            caption="cancelled bowl",
            estimate=_estimate(),
            eaten_at=datetime(2026, 1, 15, 8, tzinfo=timezone.utc),
        )
        repo.finalize_action(identity, cancelled.token, "cancel")
        for index in range(3):
            action = repo.create_pending_meal(
                identity,
                chat_id=9,
                request_message_id=31 + index,
                caption=f"confirmed bowl {index}",
                estimate=_estimate(),
                eaten_at=datetime(2026, 1, 15, 7 - index, tzinfo=timezone.utc),
            )
            repo.finalize_action(identity, action.token, "confirm")
        recent = repo.list_recent_meals(identity, limit=2)
        self.assertEqual(len(recent), 2)
        self.assertTrue(all(meal.status == "confirmed" for meal in recent))

    def test_workflow_meal_details_scaling_confirmation_and_duplicate_are_durable(self):
        identity = self.repo.resolve_identity(101, "u", "User")
        self.repo.mark_awaiting_datetime(identity)
        self.assertTrue(self.repo.set_pending_datetime(identity, "2026-01-15T04:00:00Z"))
        self.assertEqual(self.repo.get_workflow(identity.user_id)["state"], "datetime_selected")
        self.assertEqual(self.repo.consume_pending_datetime(identity), "2026-01-15T04:00:00Z")
        action = self.repo.create_pending_meal(
            identity,
            chat_id=9,
            request_message_id=10,
            caption="rice bowl",
            estimate=_estimate(),
            eaten_at=datetime(2026, 1, 15, 4, tzinfo=timezone.utc),
            update_id=777,
        )
        detail_keys = [key for key in self.table.items if key[1].startswith("MEAL_DETAIL#")]
        self.assertEqual(len(detail_keys), 2)
        scaled = self.repo.scale_action(identity, action.token, 1.2)
        self.assertEqual(round(scaled.estimate.calories), 600)
        result = self.repo.finalize_action(identity, action.token, "confirm")
        duplicate = self.repo.finalize_action(identity, action.token, "confirm")
        self.assertEqual(result.status, "confirmed")
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(result.meal.status, "confirmed")
        self.assertEqual(round(result.meal.macros.calories), 600)
        cancel_action = self.repo.create_pending_meal(
            identity,
            chat_id=9,
            request_message_id=11,
            caption="cancelled bowl",
            estimate=_estimate(),
            eaten_at=datetime(2026, 1, 15, 5, tzinfo=timezone.utc),
        )
        smaller = self.repo.scale_action(identity, cancel_action.token, 0.8)
        self.assertEqual(round(smaller.estimate.calories), 400)
        self.assertEqual(round(smaller.original_estimate.calories), 500)
        cancelled = self.repo.finalize_action(identity, cancel_action.token, "cancel")
        self.assertTrue(self.repo.finalize_action(identity, cancel_action.token, "cancel").duplicate)
        self.assertEqual(cancelled.meal.status, "cancelled")

    def test_new_photo_traceability_is_stored_without_raw_image_bytes(self):
        identity = self.repo.resolve_identity(101, "u", "User")
        action = self.repo.create_pending_meal(
            identity,
            chat_id=9,
            request_message_id=10,
            caption="traceable bowl",
            estimate=_estimate(),
            telegram_file_id="telegram-file-id",
            telegram_file_unique_id="telegram-file-unique-id",
            telegram_message_id=10,
        )
        records = [item for (pk, _sk), item in self.table.items.items() if pk == identity.pk]
        traced = [item for item in records if item.get("entity_type") in {"meal", "meal_action"}]
        self.assertEqual(len(traced), 2)
        for item in traced:
            self.assertEqual(item["telegram_file_id"], "telegram-file-id")
            self.assertEqual(item["telegram_file_unique_id"], "telegram-file-unique-id")
            self.assertEqual(item["telegram_message_id"], 10)
            self.assertNotIn("image_bytes", item)
        self.assertEqual(action.request_message_id, 10)

    def test_action_ownership_and_expiry_are_enforced(self):
        owner = self.repo.resolve_identity(101, "u", "User")
        other = self.repo.resolve_identity(202, "v", "Other")
        action = self.repo.create_pending_meal(
            owner,
            chat_id=9,
            request_message_id=12,
            caption="private bowl",
            estimate=_estimate(),
            eaten_at=datetime(2026, 1, 15, 6, tzinfo=timezone.utc),
        )
        self.assertIsNone(self.repo.get_action(other, action.token))
        with self.assertRaises(ActionNotFound):
            self.repo.finalize_action(other, action.token, "confirm")
        self.table.items[(owner.pk, f"ACTION#{action.token}")]["expires_at"] = 1
        with self.assertRaises(ActionExpired):
            self.repo.scale_action(owner, action.token, 1.2)

    def test_local_day_boundaries_are_singapore_local(self):
        start, end = local_day_utc_bounds(datetime(2026, 1, 15).date(), "Asia/Singapore")
        self.assertEqual(start, "2026-01-14T16:00:00Z")
        self.assertEqual(end, "2026-01-15T16:00:00Z")

    def test_local_day_boundaries_use_calendar_midnight_across_dst(self):
        start, end = local_day_utc_bounds(datetime(2026, 11, 1).date(), "America/New_York")
        self.assertEqual(start, "2026-11-01T04:00:00Z")
        self.assertEqual(end, "2026-11-02T05:00:00Z")

    def test_expired_workflow_is_invalid_before_dynamo_ttl_removes_it(self):
        identity = self.repo.resolve_identity(101, "u", "User")
        self.repo.mark_awaiting_datetime(identity)
        self.table.items[(identity.pk, "WORKFLOW#MEAL")]["expires_at"] = 1
        expired_repo = DynamoNutritionRepository(
            self.table,
            table_name="fitness",
            now_fn=lambda: datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
        )
        self.assertIsNone(expired_repo.get_workflow(identity.user_id))


if __name__ == "__main__":
    unittest.main()
