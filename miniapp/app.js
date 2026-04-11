const tg = window.Telegram?.WebApp ?? null;
const initData = tg?.initData ?? "";

const HOME_VIEW = "home";
const QUESTIONNAIRE_VIEW = "questionnaire";

const state = {
  meta: null,
  profile: null,
  preview: null,
  hasAuth: Boolean(initData),
  activeView: HOME_VIEW,
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
const previewEmpty = document.querySelector("#preview-empty");
const currentMeta = document.querySelector("#current-meta");
const currentMacros = document.querySelector("#current-macros");
const currentEmpty = document.querySelector("#current-empty");
const homeView = document.querySelector("#home-view");
const questionnaireView = document.querySelector("#questionnaire-view");
const openQuestionnaireButton = document.querySelector("#open-questionnaire-button");
const backHomeButton = document.querySelector("#back-home-button");
const homeCtaTitle = document.querySelector("#home-cta-title");
const homeCtaDescription = document.querySelector("#home-cta-description");
const routeChip = document.querySelector("#route-chip");
const questionnaireNote = document.querySelector("#questionnaire-note");
const questionnaireNoteCopy = document.querySelector("#questionnaire-note-copy");

if (tg) {
  tg.ready();
  tg.expand();
}

window.addEventListener("hashchange", syncRoute);

openQuestionnaireButton.addEventListener("click", () => {
  navigateTo(QUESTIONNAIRE_VIEW);
});

backHomeButton.addEventListener("click", () => {
  navigateTo(HOME_VIEW);
});

form.addEventListener("input", () => {
  state.preview = null;
  saveButton.disabled = true;
  renderQuestionnaireContext();
  renderPreview();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.hasAuth) {
    setStatus("Open this page inside Telegram to preview and save targets.", "warning");
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
    renderPreview();
    saveButton.disabled = false;
    setQuestionnaireNote("Preview ready. Review the result, then save if it looks right.", "success");
    setStatus("", "neutral");
  } catch (error) {
    setStatus(error.message || "Could not generate a preview.", "error");
  } finally {
    previewButton.disabled = false;
  }
});

saveButton.addEventListener("click", async () => {
  if (!state.hasAuth) {
    setStatus("Open this page inside Telegram to save targets.", "warning");
    return;
  }

  try {
    saveButton.disabled = true;
    const payload = collectAnswers();
    const response = await apiFetch("/miniapp/api/profile", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.profile = response.profile || null;
    state.preview = response.preview || null;
    renderCurrentProfile(state.profile);
    renderPreview();
    setQuestionnaireNote("Target saved. Recommendations will now use this profile.", "success");
    setStatus("", "neutral");
    if (tg?.HapticFeedback?.notificationOccurred) {
      tg.HapticFeedback.notificationOccurred("success");
    }
  } catch (error) {
    setStatus(error.message || "Could not save the profile.", "error");
    saveButton.disabled = false;
  }
});

bootstrap();

async function bootstrap() {
  const fallbackMeta = {
    activity_options: fallbackActivityOptions(),
    goal_options: fallbackGoalOptions(),
    activity_guidance:
      "Choose based on both exercise frequency and overall daily movement, not gym days alone.",
  };

  state.meta = fallbackMeta;
  renderMeta(fallbackMeta);
  renderCurrentProfile(null);
  syncRoute();

  setStatus(
    state.hasAuth
      ? "Loading your saved target..."
      : "Open this page from Telegram to load or save your target.",
    state.hasAuth ? "info" : "warning"
  );

  if (!state.hasAuth) {
    renderQuestionnaireContext();
    return;
  }

  try {
    const response = await apiFetch("/miniapp/api/profile");
    state.meta = response;
    state.profile = response.profile || null;
    renderMeta(response);
    renderCurrentProfile(state.profile);
    if (state.profile) {
      hydrateForm(state.profile.questionnaire_answers);
    }
    renderQuestionnaireContext();
    setStatus("", "neutral");
  } catch (error) {
    setStatus(
      error.message || "Could not load your saved target. You can still fill in the questionnaire.",
      "error"
    );
    renderQuestionnaireContext();
  }
}

function normalizeRoute(hash = window.location.hash) {
  const route = String(hash || "").replace(/^#/, "").trim().toLowerCase();
  return route === QUESTIONNAIRE_VIEW ? QUESTIONNAIRE_VIEW : HOME_VIEW;
}

function navigateTo(view) {
  const route = view === QUESTIONNAIRE_VIEW ? QUESTIONNAIRE_VIEW : HOME_VIEW;
  const targetHash = `#${route}`;
  if (window.location.hash === targetHash) {
    renderRoute(route);
    return;
  }
  window.location.hash = targetHash;
}

function syncRoute() {
  const route = normalizeRoute();
  const normalizedHash = `#${route}`;
  if (window.location.hash !== normalizedHash) {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${normalizedHash}`);
  }
  renderRoute(route);
}

function renderRoute(route) {
  state.activeView = route;
  homeView.hidden = route !== HOME_VIEW;
  questionnaireView.hidden = route !== QUESTIONNAIRE_VIEW;
  routeChip.textContent = route === HOME_VIEW ? "Home" : "Questionnaire";
  renderPreview();
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
    currentMeta.textContent = "No target saved yet";
    currentEmpty.hidden = false;
    currentMacros.hidden = true;
    currentMacros.innerHTML = "";
    homeCtaTitle.textContent = "Set up targets";
    homeCtaDescription.textContent = "Answer a short questionnaire and preview the result before saving.";
    return;
  }

  currentMeta.textContent = profile.updated_at
    ? `Saved ${formatIso(profile.updated_at)}`
    : "Saved target";
  currentEmpty.hidden = true;
  currentMacros.hidden = false;
  currentMacros.innerHTML = macroCards(profile.daily_target);
  homeCtaTitle.textContent = "Edit targets";
  homeCtaDescription.textContent = "Open the questionnaire to update your saved calories and macros.";
}

function renderPreview() {
  if (state.activeView !== QUESTIONNAIRE_VIEW) {
    previewPanel.hidden = true;
    return;
  }

  previewPanel.hidden = false;
  if (!state.preview) {
    previewSubtitle.textContent = "Use Preview target to generate the latest calculation.";
    previewEmpty.hidden = false;
    previewMacros.hidden = true;
    previewMacros.innerHTML = "";
    return;
  }

  previewEmpty.hidden = true;
  previewMacros.hidden = false;
  previewSubtitle.textContent = `${state.preview.goal_label} • ${state.preview.activity_label}`;
  previewMacros.innerHTML = macroCards(state.preview.daily_target);
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

function renderQuestionnaireContext() {
  if (!state.hasAuth) {
    setQuestionnaireNote("Preview and save only work when this page is opened inside Telegram.", "warning");
    return;
  }

  if (!state.profile) {
    setQuestionnaireNote("No saved target yet. Work through the sections below to build one.", "neutral");
    return;
  }

  if (!state.profile.questionnaire_answers) {
    setQuestionnaireNote(
      "This target came from an older migrated profile. Open the sections below only if you want to rebuild it.",
      "info"
    );
    return;
  }

  setQuestionnaireNote(
    "Saved answers loaded. Change any field, preview again, then save to replace the current target.",
    "neutral"
  );
}

function setQuestionnaireNote(message, tone = "neutral") {
  questionnaireNote.hidden = !message;
  questionnaireNote.dataset.tone = tone;
  questionnaireNoteCopy.textContent = message;
}

function setStatus(message, tone = "neutral") {
  statusPanel.hidden = !message;
  if (!message) {
    statusMessage.textContent = "";
    statusPanel.dataset.tone = "neutral";
    return;
  }
  statusMessage.textContent = message;
  statusPanel.dataset.tone = tone;
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
