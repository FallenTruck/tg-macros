.PHONY: nutrition-label nutrition-groundtruth-check nutrition-accuracy nutrition-corpus nutrition-variance e2e-nutrition-lab sync-runtime nutrition-eval deploy-miniapp e2e-install e2e-provision e2e-reset e2e-smoke e2e-screenshots

RUNTIME_DIR = lambda_handlers/runtime
AWS_PROFILE ?= fitness-dev
AWS_REGION ?= ap-southeast-1
STACK_NAME ?= tg-macros-dev
E2E_PYTHON ?= .venv/bin/python
NUTRITION_MANIFEST ?= evals/nutrition/manifest.json

.PHONY: recommendation-benchmark e2e-recommendations
recommendation-benchmark:
	$(E2E_PYTHON) -m unittest tests.test_recommendation_scenarios tests.test_post_log_messages

e2e-recommendations:
	AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" $(E2E_PYTHON) scripts/recommendation_smoke.py --live

nutrition-label:
	$(E2E_PYTHON) scripts/nutrition_label.py --manifest "$(NUTRITION_MANIFEST)" $(if $(CASE),--case "$(CASE)",) $(if $(LABEL),--input "$(LABEL)",)

nutrition-groundtruth-check:
	$(E2E_PYTHON) scripts/nutrition_label.py --check --manifest "$(NUTRITION_MANIFEST)"

nutrition-accuracy:
	$(E2E_PYTHON) scripts/nutrition_accuracy.py

sync-runtime:
	mkdir -p "$(RUNTIME_DIR)/macro_bot" lambda_handlers/webhook_runtime
	cp lambda_handlers/worker.py "$(RUNTIME_DIR)/worker.py"
	cp lambda_handlers/lab_worker.py "$(RUNTIME_DIR)/lab_worker.py"
	cp lambda_handlers/api.py "$(RUNTIME_DIR)/api.py"
	cp lambda_handlers/webhook.py lambda_handlers/webhook_runtime/webhook.py
	cp macro_bot/__init__.py macro_bot/nutrition_lab.py macro_bot/direct_estimator.py macro_bot/formatting.py macro_bot/models.py macro_bot/profile_targets.py macro_bot/recommendations.py macro_bot/serverless_auth.py macro_bot/serverless_data.py macro_bot/serverless_service.py macro_bot/workout_execution.py macro_bot/workout_programme.py "$(RUNTIME_DIR)/macro_bot/"
	cp food_catalog.json "$(RUNTIME_DIR)/food_catalog.json"

nutrition-eval:
	$(E2E_PYTHON) scripts/nutrition_eval.py

deploy-miniapp:
	AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" AWS_DEFAULT_REGION="$(AWS_REGION)" STACK_NAME="$(STACK_NAME)" bash scripts/deploy_miniapp.sh

e2e-install:
	$(E2E_PYTHON) -m pip install -r requirements-e2e.txt
	$(E2E_PYTHON) -m playwright install chromium

e2e-provision:
	AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" AWS_DEFAULT_REGION="$(AWS_REGION)" $(E2E_PYTHON) scripts/provision_e2e_account.py

e2e-reset:
	AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" AWS_DEFAULT_REGION="$(AWS_REGION)" $(E2E_PYTHON) scripts/reset_e2e_account.py

e2e-smoke:
	AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" AWS_DEFAULT_REGION="$(AWS_REGION)" $(E2E_PYTHON) scripts/reset_e2e_account.py --yes
	RUN_JAVAAN_E2E=1 AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" AWS_DEFAULT_REGION="$(AWS_REGION)" $(E2E_PYTHON) -m unittest e2e.test_live_app.LiveJavaanFitnessE2ETests.test_live_smoke

e2e-screenshots:
	AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" AWS_DEFAULT_REGION="$(AWS_REGION)" $(E2E_PYTHON) scripts/reset_e2e_account.py --yes
	RUN_JAVAAN_E2E=1 AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" AWS_DEFAULT_REGION="$(AWS_REGION)" $(E2E_PYTHON) -m unittest e2e.test_live_app.LiveJavaanFitnessE2ETests.test_live_screenshots


e2e-nutrition-lab:
	AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" AWS_DEFAULT_REGION="$(AWS_REGION)" $(E2E_PYTHON) scripts/reset_e2e_account.py --yes
	RUN_JAVAAN_E2E=1 AWS_PROFILE="$(AWS_PROFILE)" AWS_REGION="$(AWS_REGION)" AWS_DEFAULT_REGION="$(AWS_REGION)" $(E2E_PYTHON) -m unittest e2e.test_live_app.LiveJavaanFitnessE2ETests.test_live_nutrition_lab


nutrition-corpus:
	$(E2E_PYTHON) scripts/nutrition_variance.py

nutrition-variance:
	$(E2E_PYTHON) scripts/nutrition_variance.py --report
