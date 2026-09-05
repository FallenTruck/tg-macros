"""Shared user-visible Profile flow for offline and synthetic live Chromium."""
from pathlib import Path


def exercise_profile_settings(page, screenshot_dir=None):
    page.get_by_test_id('nav-profile').click()
    page.locator('#nutrition-settings-form').wait_for(state='visible')
    fields = {'diet_type': 'vegetarian', 'eggs_allowed': 'true', 'dairy_allowed': 'false',
              'preferred_cuisines': 'Indian, Asian', 'preferred_meal_styles': 'light, high protein',
              'forbidden_ingredients': 'peanut', 'recommendation_bedtime': '22:45', 'variety_preference': 'high'}

    def fill(values):
        for key, value in values.items():
            field = page.locator('#setting-' + key)
            if key in ('diet_type', 'eggs_allowed', 'dairy_allowed', 'variety_preference'):
                field.select_option(value)
            else:
                field.fill(value)

    def save():
        page.locator('#save-nutrition-settings').click()
        page.wait_for_function("document.querySelector('#nutrition-settings-status').textContent === 'Food settings saved.'")

    def reload_and_check(values):
        page.reload(wait_until='domcontentloaded')
        page.locator('#nutrition-settings-form').wait_for(state='visible', timeout=30000)
        for key, value in values.items():
            assert page.locator('#setting-' + key).input_value() == value, f'{key} did not persist'

    fill(fields)
    page.locator('#setting-forbidden_ingredients').fill('x' * 101)
    page.locator('#save-nutrition-settings').click()
    page.wait_for_function("document.querySelector('#nutrition-settings-status').dataset.tone === 'error'")
    page.locator('#setting-forbidden_ingredients').fill(fields['forbidden_ingredients'])
    save()
    reload_and_check(fields)
    alternate = {**fields, 'diet_type': 'non_vegetarian', 'eggs_allowed': 'false', 'dairy_allowed': 'true'}
    fill(alternate)
    save()
    reload_and_check(alternate)
    fill(fields)
    save()
    # Ordinary target recalculation must omit and preserve the food settings.
    page.locator('#profile-edit-button').click()
    page.locator('#weight_kg').fill('81')
    page.locator('#preview-button').click()
    page.locator('#save-button').wait_for(state='visible')
    page.wait_for_function("!document.querySelector('#save-button').disabled")
    page.locator('#save-button').click()
    page.wait_for_function("document.querySelector('#questionnaire-note-copy').textContent.startsWith('Target saved.')")
    page.get_by_test_id('nav-profile').click()
    reload_and_check(fields)
    for width, label in ((360, 'mobile'), (1280, 'desktop')):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Profile overflow at {width}'
        if screenshot_dir:
            path = Path(screenshot_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path / f'profile-v2-{label}.png'), full_page=True)
            page.locator('#nutrition-settings-panel').evaluate("element => window.scrollTo(0, element.offsetTop - 16)")
            page.screenshot(path=str(path / f'profile-v2-diet-{label}.png'))
            page.locator('#setting-recommendation_bedtime').scroll_into_view_if_needed()
            page.screenshot(path=str(path / f'profile-v2-timing-{label}.png'))
