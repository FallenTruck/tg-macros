import copy
import unittest

from scripts.e2e_support import (
    BASELINE_PROFILE_PAYLOAD,
    E2E_IDENTITY_PK,
    E2E_USERNAME,
    E2E_USER_ID,
    E2EAccountSafetyError,
    identity_item,
    marker_item,
    validate_e2e_credential,
    validate_e2e_records,
)
from scripts.provision_e2e_account import generate_e2e_password
from scripts.provision_e2e_account import ensure_e2e_identity
from scripts.reset_e2e_account import delete_user_partition, reset_e2e_account


class _BatchWriter:
    def __init__(self, table):
        self.table = table

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def delete_item(self, *, Key):
        self.table.deleted.append((Key["PK"], Key["SK"]))


class _PartitionTable:
    def __init__(self, items):
        self.items = items
        self.deleted = []
        self.queried = False
        self.scanned = False

    def query(self, **kwargs):
        self.queried = True
        return {"Items": copy.deepcopy(self.items)}

    def batch_writer(self):
        return _BatchWriter(self)

    def scan(self, **_kwargs):
        self.scanned = True
        raise AssertionError("E2E reset must never scan the table")


class _AccountTable:
    def __init__(self, records):
        self.records = {(item["PK"], item["SK"]): item for item in records}
        self.batch_called = False

    def get_item(self, **kwargs):
        return {"Item": self.records.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))}

    def query(self, **_kwargs):
        return {"Items": [item for item in self.records.values() if str(item.get("PK", "")).startswith("USER#")]}

    def batch_writer(self):
        self.batch_called = True
        raise AssertionError("unsafe reset must stop before deleting")


class _ProvisionRepository:
    table_name = "fitness"

    def __init__(self):
        self.transaction_called = False

    def _transact_write(self, _operations):
        self.transaction_called = True


class E2EAccountToolTests(unittest.TestCase):
    def test_reserved_marker_and_identity_are_explicitly_e2e(self):
        marker = marker_item()
        identity = identity_item()
        validate_e2e_records(marker, identity)
        self.assertEqual(marker["account_type"], "e2e")
        self.assertEqual(identity["PK"], E2E_IDENTITY_PK)
        self.assertEqual(identity["telegram_user_id"], 0)

    def test_marker_validation_rejects_a_normal_identity(self):
        normal_identity = identity_item()
        normal_identity["account_type"] = "telegram"
        with self.assertRaises(E2EAccountSafetyError):
            validate_e2e_records(marker_item(), normal_identity)

    def test_reset_refuses_a_normal_account_before_any_delete(self):
        normal_identity = identity_item()
        normal_identity["account_type"] = "telegram"
        table = _AccountTable([marker_item(), normal_identity])
        repository = type("Repository", (), {"get_web_credential": lambda *_args: None})()
        with self.assertRaises(E2EAccountSafetyError):
            reset_e2e_account(table=table, repository=repository)
        self.assertFalse(table.batch_called)

    def test_provisioning_refuses_a_reserved_user_partition_collision(self):
        table = _AccountTable(
            [{"PK": f"USER#{E2E_USER_ID}", "SK": "PROFILE", "entity_type": "profile"}]
        )
        repository = _ProvisionRepository()
        with self.assertRaises(E2EAccountSafetyError):
            ensure_e2e_identity(table, repository)
        self.assertFalse(repository.transaction_called)

    def test_credential_validation_rejects_a_normal_user_mapping(self):
        credential = {
            "entity_type": "web_credential",
            "username": E2E_USERNAME,
            "user_id": "normal-user",
            "telegram_user_id": 101,
            "identity_pk": "IDENTITY#TELEGRAM#101",
        }
        with self.assertRaises(E2EAccountSafetyError):
            validate_e2e_credential(credential)

    def test_reset_deletes_only_the_exact_e2e_user_partition(self):
        table = _PartitionTable(
            [
                {"PK": f"USER#{E2E_USER_ID}", "SK": "PROFILE"},
                {"PK": f"USER#{E2E_USER_ID}", "SK": "WORKOUT#ACTIVE"},
            ]
        )
        deleted = delete_user_partition(table, E2E_USER_ID)
        self.assertEqual(deleted, 2)
        self.assertEqual(
            table.deleted,
            [(f"USER#{E2E_USER_ID}", "PROFILE"), (f"USER#{E2E_USER_ID}", "WORKOUT#ACTIVE")],
        )
        self.assertTrue(table.queried)
        self.assertFalse(table.scanned)

    def test_generated_password_is_random_and_not_part_of_hash_metadata(self):
        first = generate_e2e_password()
        second = generate_e2e_password()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 32)
        self.assertGreaterEqual(len(second), 32)
        from macro_bot.serverless_auth import hash_web_password

        record = hash_web_password(first)
        self.assertNotIn(first, str(record))
        self.assertNotIn(second, str(record))

    def test_ssm_names_and_profile_baseline_are_stable(self):
        from scripts.e2e_support import E2E_PASSWORD_PARAMETER, E2E_USERNAME_PARAMETER

        self.assertEqual(E2E_USERNAME_PARAMETER, "/tg-macros/dev/e2e/web_username")
        self.assertEqual(E2E_PASSWORD_PARAMETER, "/tg-macros/dev/e2e/web_password")
        self.assertEqual(BASELINE_PROFILE_PAYLOAD["timezone"], "Asia/Singapore")
        self.assertEqual(BASELINE_PROFILE_PAYLOAD["goal"], "maintain")


if __name__ == "__main__":
    unittest.main()
