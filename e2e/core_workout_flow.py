"""Exercise optional core loads through the actual Mini App controls."""
from pathlib import Path


def exercise_core_choices(page, output_dir):
    from playwright.sync_api import expect
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    page.locator('[data-route="workout"]').click()
    for choice in ("standing_ab_crunch_machine", "russian_twist"):
        page.get_by_test_id("workout-day-start-PUSH").click()
        page.get_by_test_id("active-workout").wait_for(state="visible")
        core = page.locator('article[data-testid^="workout-execution-"]').last
        core.locator('[data-action="choose-exercise"]').select_option(choice)
        weight = core.locator('input[name="load_value"]')
        expect(weight).to_have_attribute("placeholder", "Bodyweight")
        assert weight.get_attribute("required") is None
        core.locator('input[name="reps"]').fill("8")
        core.get_by_test_id("workout-save-set").click()
        expect(core.locator('.workout-set-list')).to_contain_text("Bodyweight × 8 reps")
        core.locator('input[name="load_value"]').fill("5")
        core.locator('input[name="reps"]').fill("12")
        core.get_by_test_id("workout-save-set").click()
        expect(core.locator('.workout-set-list')).to_contain_text("5 kg × 12")
        page.reload(wait_until="domcontentloaded")
        page.get_by_test_id("active-workout").wait_for(state="visible")
        core = page.locator('article[data-testid^="workout-execution-"]').last
        expect(core.locator('[data-action="choose-exercise"]')).to_have_value(choice)
        expect(core.locator('.workout-set-list')).to_contain_text("Bodyweight × 8 reps")
        expect(core.locator('.workout-set-list')).to_contain_text("5 kg × 12")
        core.scroll_into_view_if_needed()
        core.screenshot(path=str(output / f"{choice}-mobile.png"))
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.locator('[data-action="cancel-workout"]').click()
        page.get_by_test_id("workout-day-start-PUSH").wait_for(state="visible")
