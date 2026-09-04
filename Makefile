.PHONY: sync-runtime deploy-miniapp

RUNTIME_DIR = lambda_handlers/runtime
AWS_PROFILE ?= fitness-dev
AWS_REGION ?= ap-southeast-1
STACK_NAME ?= tg-macros-dev

sync-runtime:
	mkdir -p "$(RUNTIME_DIR)/macro_bot" lambda_handlers/webhook_runtime
	cp lambda_handlers/worker.py "$(RUNTIME_DIR)/worker.py"
	cp lambda_handlers/api.py "$(RUNTIME_DIR)/api.py"
	cp lambda_handlers/webhook.py lambda_handlers/webhook_runtime/webhook.py
	cp macro_bot/__init__.py macro_bot/direct_estimator.py macro_bot/formatting.py macro_bot/models.py macro_bot/profile_targets.py macro_bot/recommendations.py macro_bot/serverless_auth.py macro_bot/serverless_data.py macro_bot/serverless_service.py macro_bot/workout_execution.py macro_bot/workout_programme.py "$(RUNTIME_DIR)/macro_bot/"
	cp food_catalog.json "$(RUNTIME_DIR)/food_catalog.json"

deploy-miniapp:
	AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" AWS_DEFAULT_REGION="$(AWS_REGION)" STACK_NAME="$(STACK_NAME)" bash scripts/deploy_miniapp.sh
