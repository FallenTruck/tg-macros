const tg = window.Telegram?.WebApp ?? null;
const initData = tg?.initData ?? "";

const state = {
  meta: null,
  preview: null,
  hasAuth: Boolean(initData),
};

const form = document.querySelector("#questionnaire-form");
const statusPanel = document.querySelector("#status-panel");
const statusMessage = document.querySelector("#status-message");
const activityOptionsEl = document.querySelector("#activity-options");
const goalOptionsEl = document.querySelector("#goal-options");
const activityGuidanceEl = document.querySelector("#activity-guidance");
const previewButton = document.querySelector("#preview-button");
const saveButton = document.querySelector("#save-button");
const previewPanel = document.querySelector("#preview-panel");
const previewSubtitle = document.querySelector("#preview-subtitle");
const previewMacros = document.querySelector("#preview-macros");
const currentPanel = document.querySelector("#current-panel");
const currentMeta = document.querySelector("#current-meta");
const currentMacros = document.querySelector("#current-macros");

if (tg) {
  tg.ready();
  tg.expand();
}

bootstrap();

form.addEventListener("input", () => {
  state.preview = null;
  saveButton.disabled = true;
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.hasAuth) {
    setStatus("Open this page inside Telegram to preview and save targets.", true);
    return;
  }

  try {
    previewButton.disabled = true;
    const payload = collectAnswers();
    const response = await apiFetch("/miniapp/api/targets/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.preview = response;
    renderPreview(response);
    saveButton.disabled = false;
    setStatus("Preview ready. Review the numbers, then save.", false);
  } catch (error) {
    setStatus(error.message || "Could not generate a preview.", true);
  } finally {
    previewButton.disabled = false;
  }
});

saveButton.addEventListener("click", async () => {
  if (!state.hasAuth) {
    setStatus("Open this page inside Telegram to save targets.", true);
    return;
  }

  try {
    saveButton.disabled = true;
    const payload = collectAnswers();
    const response = await apiFetch("/miniapp/api/profile", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderCurrentProfile(response.profile);
    if (response.preview) {
      renderPreview(response.preview);
    }
    setStatus("Targets saved. Recommendations will now use this profile.", false);
    if (tg?.HapticFeedback?.notificationOccurred) {
      tg.HapticFeedback.notificationOccurred("success");
    }
  } catch (error) {
    setStatus(error.message || "Could not save the profile.", true);
    saveButton.disabled = false;
  }
});

async function bootstrap() {
  const fallbackMeta = {
    activity_options: fallbackActivityOptions(),
    goal_options: fallbackGoalOptions(),
    activity_guidance:
      "Choose based on both exercise frequency and overall daily movement, not gym days alone.",
  };
  renderMeta(fallbackMeta);

  setStatus(
    state.hasAuth
      ? "Loading your saved target..."
      : "Open this page from Telegram to load or save your target.",
    !state.hasAuth
  );

  if (!state.hasAuth) {
    return;
  }

  try {
    const response = await apiFetch("/miniapp/api/profile");
    state.meta = response;
    renderMeta(response);
    if (response.profile) {
      renderCurrentProfile(response.profile);
      hydrateForm(response.profile.questionnaire_answers);
      if (!response.profile.questionnaire_answers) {
        setStatus(
          "A migrated target already exists. Raw questionnaire answers were not available, so the form starts blank.",
          false
        );
      } else {
        setStatus("Saved target loaded. Adjust answers if you want to recalculate.", false);
      }
    } else {
      setStatus("No saved target yet. Fill in the questionnaire to create one.", false);
    }
  } catch (error) {
    setStatus(
      error.message || "Could not load your saved target. You can still fill in the questionnaire.",
      true
    );
  }
}

function renderMeta(meta) {
  const activityOptions = meta.activity_options || fallbackActivityOptions();
  const goalOptions = meta.goal_options || fallbackGoalOptions();
  activityGuidanceEl.textContent = meta.activity_guidance || "";

  activityOptionsEl.innerHTML = activityOptions
    .map(
      (item, index) => `
        <label class="choice-card">
          <input
            type="radio"
            name="activity_level"
            value="${escapeHtml(item.value)}"
            ${index === 2 ? "checked" : ""}
            required
          />
          <span class="choice-title">${escapeHtml(item.label)}</span>
          <span class="choice-copy">${escapeHtml(item.description)}</span>
        </label>
      `
    )
    .join("");

  goalOptionsEl.innerHTML = goalOptions
    .map(
      (item, index) => `
        <label class="goal-chip">
          <input
            type="radio"
            name="goal"
            value="${escapeHtml(item.value)}"
            ${index === 1 ? "checked" : ""}
            required
          />
          <span>${escapeHtml(item.label)}</span>
        </label>
      `
    )
    .join("");
}

function renderCurrentProfile(profile) {
  if (!profile) {
    currentPanel.hidden = true;
    return;
  }

  currentPanel.hidden = false;
  currentMeta.textContent = profile.updated_at
    ? `Saved ${formatIso(profile.updated_at)}`
    : "Saved target";
  currentMacros.innerHTML = macroCards(profile.daily_target);
}

function renderPreview(preview) {
  previewPanel.hidden = false;
  previewSubtitle.textContent = `${preview.goal_label} • ${preview.activity_label}`;
  previewMacros.innerHTML = macroCards(preview.daily_target);
}

function hydrateForm(answers) {
  if (!answers) {
    document.querySelector("#sex").value = "male";
    return;
  }

  document.querySelector("#sex").value = answers.sex;
  document.querySelector("#age_years").value = answers.age_years;
  document.querySelector("#height_cm").value = answers.height_cm;
  document.querySelector("#weight_kg").value = answers.weight_kg;

  const activityInput = form.querySelector(`input[name="activity_level"][value="${answers.activity_level}"]`);
  if (activityInput) {
    activityInput.checked = true;
  }
  const goalInput = form.querySelector(`input[name="goal"][value="${answers.goal}"]`);
  if (goalInput) {
    goalInput.checked = true;
  }
}

function collectAnswers() {
  const formData = new FormData(form);
  return {
    sex: String(formData.get("sex") || "").trim(),
    age_years: Number(formData.get("age_years")),
    height_cm: Number(formData.get("height_cm")),
    weight_kg: Number(formData.get("weight_kg")),
    activity_level: String(formData.get("activity_level") || "").trim(),
    goal: String(formData.get("goal") || "").trim(),
  };
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    method: options.method || "GET",
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": initData,
      ...(options.headers || {}),
    },
    body: options.body,
  });

  if (!response.ok) {
    let detail = "Request failed.";
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch (_error) {
      detail = response.statusText || detail;
    }
    throw new Error(detail);
  }

  return response.json();
}

function macroCards(target) {
  return `
    ${macroCard("Calories", `${Math.round(target.calories)} kcal`)}
    ${macroCard("Protein", `${target.protein_g.toFixed(1)} g`)}
    ${macroCard("Carbs", `${target.carbs_g.toFixed(1)} g`)}
    ${macroCard("Fat", `${target.fat_g.toFixed(1)} g`)}
  `;
}

function macroCard(label, value) {
  return `
    <article class="macro-card">
      <span class="macro-label">${escapeHtml(label)}</span>
      <strong class="macro-value">${escapeHtml(value)}</strong>
    </article>
  `;
}

function setStatus(message, isError) {
  statusMessage.textContent = message;
  statusPanel.dataset.tone = isError ? "error" : "neutral";
}

function formatIso(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fallbackActivityOptions() {
  return [
    {
      value: "sedentary",
      label: "Sedentary (little or no exercise)",
      description: "Mostly seated lifestyle, minimal training, low day-to-day movement.",
    },
    {
      value: "light",
      label: "Lightly active (exercise 1-2 days/week)",
      description: "Light training or decent walking, but not consistently active most days.",
    },
    {
      value: "moderate",
      label: "Moderately active (exercise 3-4 days/week)",
      description: "Regular moderate training and average day-to-day movement.",
    },
    {
      value: "active",
      label: "Active (exercise 5-6 days/week)",
      description: "Hard training most days or a physically active routine/job.",
    },
    {
      value: "very_active",
      label: "Very active (daily intense training or physical job)",
      description: "Very high activity from intense daily exercise, double sessions, or sustained physical work.",
    },
  ];
}

function fallbackGoalOptions() {
  return [
    { value: "lose", label: "Lose fat" },
    { value: "maintain", label: "Maintain" },
    { value: "gain", label: "Gain muscle" },
  ];
}
