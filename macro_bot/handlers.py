import io
import logging
from typing import Optional
from uuid import uuid4

from telegram import Message, Update
from telegram.ext import ContextTypes

from .catalog_growth import CatalogGrowthService
from .config import BotConfig
from .formatting import (
    build_meal_keyboard,
    build_setup_keyboard,
    format_pending_message,
    format_profile_setup_message,
    format_recommendation_message,
    parse_meal_datetime,
)
from .identity import legacy_person_for_user
from .metrics import (
    append_outcome_event,
    append_recommendation_event,
    append_recommendation_skip_event,
)
from .models import LoggedMealRow, PendingMealAction
from .recommendations import RecommendationPlanner
from .services import MacroEstimatorClient, MacroEstimatorError
from .state import MealWorkflowStore
from .storage import MealLogRepository

logger = logging.getLogger(__name__)


class BotHandlers:
    _SETUP_START_PARAM = "macro_setup"

    def __init__(
        self,
        config: BotConfig,
        estimator_client: MacroEstimatorClient,
        meal_log_repository: MealLogRepository,
        recommendation_planner: RecommendationPlanner,
        catalog_growth_service: Optional[CatalogGrowthService] = None,
    ):
        self._config = config
        self._estimator_client = estimator_client
        self._meal_log_repository = meal_log_repository
        self._recommendation_planner = recommendation_planner
        self._catalog_growth_service = catalog_growth_service

    def _store(self, context: ContextTypes.DEFAULT_TYPE) -> MealWorkflowStore:
        store = MealWorkflowStore(
            bot_data=context.application.bot_data,
            action_ttl_seconds=self._config.pending_action_ttl_seconds,
            datetime_ttl_seconds=self._config.pending_datetime_ttl_seconds,
        )
        store.cleanup()
        return store

    async def on_logmeal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return

        self._store(context).mark_awaiting_datetime(user.id)
        await msg.reply_text("Send meal date/time in this format: DD-MM-YYYY HH:MM")

    async def on_datetime_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return

        store = self._store(context)
        if not store.is_awaiting_datetime(user.id):
            return

        raw_text = (msg.text or "").strip()
        try:
            parsed_dt = parse_meal_datetime(raw_text, self._config.date_input_format)
        except ValueError:
            await msg.reply_text("Invalid format. Use: DD-MM-YYYY HH:MM")
            return

        store.set_pending_datetime(user.id, parsed_dt.isoformat(timespec="seconds"))
        await msg.reply_text(f"✅ Meal time set: {raw_text}. Now send the meal photo.")

    async def on_openapp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return

        setup_url = await self._profile_setup_url(context)
        await msg.reply_text(
            format_profile_setup_message(setup_url),
            reply_markup=build_setup_keyboard(setup_url),
        )

    async def on_suggestmeal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return

        await self._send_recommendation_message(
            telegram_user_id=user.id,
            trigger_source="command",
            context=context,
            reply_message=msg,
        )

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None or not msg.photo:
            return

        caption = msg.caption or ""

        photo = msg.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)

        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        image_bytes = buf.getvalue()

        store = self._store(context)
        persona_hint = store.get_persona_hint(user.id, caption)

        try:
            estimate = await self._estimator_client.estimate(
                image_bytes=image_bytes,
                caption=caption,
                persona_hint=persona_hint,
            )
        except MacroEstimatorError as err:
            await msg.reply_text(
                "❌ Macro estimation failed (API error).\n"
                "Try again in a moment, or resend with a clearer caption.\n"
                f"Detail: {str(err)[:120]}"
            )
            return

        token = uuid4().hex[:12]
        assigned_dt = store.pop_pending_datetime(user.id)

        action = PendingMealAction(
            token=token,
            chat_id=msg.chat_id,
            request_message_id=msg.message_id,
            telegram_user_id=user.id,
            username=user.username,
            datetime_iso=assigned_dt,
            caption=caption,
            estimate=estimate,
            metrics_event_id=estimate.metrics_event_id,
        )
        store.add_action(action)

        estimate_msg = await msg.reply_text(
            format_pending_message(action),
            reply_markup=build_meal_keyboard(token),
        )
        action.message_id = estimate_msg.message_id

    async def on_meal_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query is None:
            return

        data = query.data or ""
        parts = data.split(":")
        if len(parts) != 4:
            await query.answer()
            return

        action_name = parts[2]
        token = parts[3]

        store = self._store(context)
        meal_action = store.get_action(token)
        if meal_action is None:
            await query.answer("Meal expired.")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                # Message may already be finalized or not editable; ignore safely.
                pass
            return

        actor_id = update.effective_user.id if update.effective_user else None
        if actor_id != meal_action.telegram_user_id:
            await query.answer("Not your meal.")
            return

        if action_name == "smaller":
            try:
                meal_action.scale(0.8)
            except ValueError as err:
                await query.answer(str(err))
                return

            await query.answer()
            await query.edit_message_text(
                format_pending_message(meal_action),
                reply_markup=build_meal_keyboard(token),
            )
            return

        if action_name == "larger":
            try:
                meal_action.scale(1.2)
            except ValueError as err:
                await query.answer(str(err))
                return

            await query.answer()
            await query.edit_message_text(
                format_pending_message(meal_action),
                reply_markup=build_meal_keyboard(token),
            )
            return

        if action_name == "confirm":
            try:
                meal_action.confirm()
            except ValueError as err:
                await query.answer(str(err))
                return

            await query.answer()
            person = self._resolve_legacy_person(meal_action)
            row = LoggedMealRow.from_pending(meal_action, person=person)
            self._meal_log_repository.append(row)
            store.record_confirmed_meal(meal_action.telegram_user_id, meal_action.caption, meal_action.estimate)
            try:
                append_outcome_event(
                    action=meal_action,
                    final_action="confirmed",
                    test_label="actual",
                    person=person,
                )
            except Exception as metrics_err:
                logger.warning("outcome_metrics_logging_failed detail=%s", str(metrics_err)[:120])

            logger.info(
                "meal_confirmation user_id=%s adjustment_factor=%.3f caption=%s calories=%s range=%s-%s drivers=%s",
                meal_action.telegram_user_id,
                meal_action.adjustment_factor,
                meal_action.caption[:80],
                int(round(meal_action.estimate.calories)),
                int(round(meal_action.estimate.total_low.calories)) if meal_action.estimate.total_low else None,
                int(round(meal_action.estimate.total_high.calories)) if meal_action.estimate.total_high else None,
                "; ".join(meal_action.estimate.variance_drivers[:3]),
            )

            await query.edit_message_text(
                f"{format_pending_message(meal_action)}\n\n✅ Logged",
                reply_markup=None,
            )
            if self._catalog_growth_service is not None:
                try:
                    await self._catalog_growth_service.refresh_with_review(
                        telegram_user_id=meal_action.telegram_user_id
                    )
                except Exception as err:
                    logger.warning(
                        "catalog_growth_refresh_failed telegram_user_id=%s detail=%s",
                        meal_action.telegram_user_id,
                        str(err)[:120],
                    )
            await self._send_recommendation_message(
                telegram_user_id=meal_action.telegram_user_id,
                trigger_source="auto_confirm",
                context=context,
                chat_id=meal_action.chat_id,
            )
            return

        if action_name == "cancel":
            try:
                meal_action.cancel()
            except ValueError as err:
                await query.answer(str(err))
                return

            await query.answer()
            person = self._resolve_legacy_person(meal_action)
            try:
                append_outcome_event(
                    action=meal_action,
                    final_action="cancelled",
                    test_label="test",
                    person=person,
                )
            except Exception as metrics_err:
                logger.warning("outcome_metrics_logging_failed detail=%s", str(metrics_err)[:120])
            await query.edit_message_text(
                (
                    f"{format_pending_message(meal_action)}\n\n"
                    "❌ Cancelled — please re-upload the photo (add caption if possible)."
                ),
                reply_markup=None,
            )
            return

        await query.answer()

    @staticmethod
    def _resolve_legacy_person(action: PendingMealAction) -> str:
        return legacy_person_for_user(action.telegram_user_id, username=action.username) or "unknown"

    async def _send_recommendation_message(
        self,
        telegram_user_id: int,
        trigger_source: str,
        context: ContextTypes.DEFAULT_TYPE,
        reply_message: Optional[Message] = None,
        chat_id: Optional[int] = None,
    ) -> None:
        try:
            result, prepared = await self._recommendation_planner.recommend_next_meal(
                telegram_user_id=telegram_user_id
            )
        except KeyError:
            setup_url = await self._profile_setup_url(context)
            await self._deliver_message(
                format_profile_setup_message(setup_url),
                context=context,
                reply_message=reply_message,
                chat_id=chat_id,
                reply_markup=build_setup_keyboard(setup_url),
            )
            return
        except Exception as err:
            logger.warning(
                "recommendation_flow_failed telegram_user_id=%s detail=%s",
                telegram_user_id,
                str(err)[:120],
            )
            await self._deliver_message(
                "Could not generate meal suggestions right now.",
                context=context,
                reply_message=reply_message,
                chat_id=chat_id,
            )
            return

        try:
            if result.suggestions:
                append_recommendation_event(
                    telegram_user_id=telegram_user_id,
                    trigger_source=trigger_source,
                    prepared=prepared,
                    result=result,
                )
            else:
                append_recommendation_skip_event(
                    telegram_user_id=telegram_user_id,
                    trigger_source=trigger_source,
                    prepared=prepared,
                )
        except Exception as metrics_err:
            logger.warning(
                "recommendation_metrics_logging_failed telegram_user_id=%s detail=%s",
                telegram_user_id,
                str(metrics_err)[:120],
            )

        await self._deliver_message(
            format_recommendation_message(result),
            context=context,
            reply_message=reply_message,
            chat_id=chat_id,
        )

    @staticmethod
    async def _deliver_message(
        text: str,
        context: ContextTypes.DEFAULT_TYPE,
        reply_message: Optional[Message] = None,
        chat_id: Optional[int] = None,
        reply_markup=None,
    ) -> None:
        if reply_message is not None:
            await reply_message.reply_text(text, reply_markup=reply_markup)
            return
        if chat_id is not None:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    async def _profile_setup_url(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        username = getattr(context.bot, "username", "")
        if isinstance(username, str):
            username = username.strip().lstrip("@")
        else:
            username = ""

        if not username:
            try:
                me = await context.bot.get_me()
            except Exception:
                me = None
            fetched_username = getattr(me, "username", "") if me is not None else ""
            if isinstance(fetched_username, str):
                username = fetched_username.strip().lstrip("@")

        if username:
            return f"https://t.me/{username}?startapp={self._SETUP_START_PARAM}"
        return self._config.mini_app_url
