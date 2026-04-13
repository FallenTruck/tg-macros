const tg = window.Telegram?.WebApp ?? null;
const initData = tg?.initData ?? "";

const HOME_VIEW = "home";
const PROFILE_VIEW = "profile";
const QUESTIONNAIRE_VIEW = "questionnaire";

const state = {
  meta: null,
  profile: null,
  preview: null,
  viewer: buildViewerFromTelegram(tg?.initDataUnsafe?.user ?? null),
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

const homeView = document.querySelector("#home-view");
const profileView = document.querySelector("#profile-view");
const questionnaireView = document.querySelector("#questionnaire-view");

const welcomeTitle = document.querySelector("#welcome-title");
const welcomeHandle = document.querySelector("#welcome-handle");
const homeSummaryTitle = document.querySelector("#home-summary-title");
const homeSummaryMeta = document.querySelector("#home-summary-meta");
const homeSummaryEmpty = document.querySelector("#home-summary-empty");
const homeSummaryMacros = document.querySelector("#home-summary-macros");
const openProfileButton = document.querySelector("#open-profile-button");

const viewerInitial = document.querySelector("#viewer-initial");
const profileViewerTitle = document.querySelector("#profile-viewer-title");
const profileViewerSubtitle = document.querySelector("#profile-viewer-subtitle");
const profileSummaryTitle = document.querySelector("#profile-summary-title");
const profileMeta = document.querySelector("#profile-meta");
const profileEmpty = document.querySelector("#profile-empty");
const profileMacros = document.querySelector("#profile-macros");
const profileEditButton = document.querySelector("#profile-edit-button");
const backProfileButton = document.querySelector("#back-profile-button");

const questionnaireNote = document.querySelector("#questionnaire-note");
const questionnaireNoteCopy = document.querySelector("#questionnaire-note-copy");

const navButtons = Array.from(document.querySelectorAll(".nav-item"));

if (tg) {
  tg.ready();
  tg.expand();
}

window.addEventListener("hashchange", syncRoute);

openProfileButton.addEventListener("click", () => {
  navigateTo(PROFILE_VIEW);
});

profileEditButton.addEventListener("click", () => {
  navigateTo(QUESTIONNAIRE_VIEW);
});

backProfileButton.addEventListener("click", () => {
  navigateTo(PROFILE_VIEW);
});

for (const button of navButtons) {
  button.addEventListener("click", () => {
    const route = button.dataset.route === PROFILE_VIEW ? PROFILE_VIEW : HOME_VIEW;
    navigateTo(route);
  });
}

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
    saveButton.disabled = false;
    setQuestionnaireNote("Preview ready. Review the result, then save if it looks right.", "success");
    setStatus("", "neutral");
    renderPreview();
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
    state.viewer = normalizeViewer(response.viewer, state.viewer);
    state.preview = response.preview || null;
    renderViewer();
    renderHomeSummary();
    renderProfileSummary();
    renderQuestionnaireContext();
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
  renderViewer();
  renderHomeSummary();
  renderProfileSummary();
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
    state.viewer = normalizeViewer(response.viewer, state.viewer);
    renderMeta(response);
    renderViewer();
    renderHomeSummary();
    renderProfileSummary();
    if (state.profile?.questionnaire_answers) {
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

function buildViewerFromTelegram(user) {
  if (!user || typeof user !== "object") {
    return { telegram_user_id: 0, username: "", display_name: "" };
  }
  const username = String(user.username || "").trim();
  const firstName = String(user.first_name || "").trim();
  const lastName = String(user.last_name || "").trim();
  const displayName = [firstName, lastName].filter(Boolean).join(" ").trim() || username;
  return {
    telegram_user_id: Number(user.id || 0),
    username,
    display_name: displayName,
  };
}

function normalizeViewer(viewer, fallback = null) {
  const source = viewer && typeof viewer === "object" ? viewer : fallback || {};
  return {
    telegram_user_id: Number(source.telegram_user_id || 0),
    username: String(source.username || "").trim(),
    display_name: String(source.display_name || "").trim(),
  };
}

function viewerPrimaryLabel(viewer) {
  if (viewer.username) {
    return `@${viewer.username}`;
  }
  if (viewer.display_name) {
    return viewer.display_name;
  }
  return "there";
}

function viewerSecondaryLabel(viewer) {
  if (viewer.username && viewer.display_name && viewer.display_name !== viewer.username) {
    return viewer.display_name;
  }
  if (viewer.username) {
    return "Telegram profile connected";
  }
  if (viewer.display_name) {
    return "Telegram display name loaded";
  }
  return "Open this Mini App from Telegram to load your identity.";
}

function viewerInitialValue(viewer) {
  const raw = viewer.display_name || viewer.username || "J";
  return raw.slice(0, 1).toUpperCase();
}

function normalizeRoute(hash = window.location.hash) {
  const route = String(hash || "").replace(/^#/, "").trim().toLowerCase();
  if (route === PROFILE_VIEW || route === QUESTIONNAIRE_VIEW) {
    return route;
  }
  return HOME_VIEW;
}

function navigateTo(view) {
  const route = view === QUESTIONNAIRE_VIEW || view === PROFILE_VIEW ? view : HOME_VIEW;
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
  profileView.hidden = route !== PROFILE_VIEW;
  questionnaireView.hidden = route !== QUESTIONNAIRE_VIEW;

  for (const button of navButtons) {
    const buttonRoute = button.dataset.route === PROFILE_VIEW ? PROFILE_VIEW : HOME_VIEW;
    const isActive = buttonRoute === HOME_VIEW
      ? route === HOME_VIEW
      : route === PROFILE_VIEW || route === QUESTIONNAIRE_VIEW;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-current", isActive ? "page" : "false");
  }

  renderPreview();
}

function renderViewer() {
  const viewer = normalizeViewer(state.viewer);
  const primary = viewerPrimaryLabel(viewer);
  const secondary = viewerSecondaryLabel(viewer);

  welcomeTitle.textContent = primary === "there" ? "Hello there" : `Hello ${primary}`;
  welcomeHandle.textContent = secondary;

  viewerInitial.textContent = viewerInitialValue(viewer);
  profileViewerTitle.textContent = primary === "there" ? "Profile" : primary;
  profileViewerSubtitle.textContent = secondary === "Open this Mini App from Telegram to load your identity."
    ? "Manage your saved target here."
    : secondary;
}

function renderHomeSummary() {
  if (!state.profile) {
    homeSummaryTitle.textContent = "No target saved yet";
    homeSummaryMeta.textContent = "Profile setup lives under Profile.";
    homeSummaryEmpty.hidden = false;
    homeSummaryMacros.hidden = true;
    homeSummaryMacros.innerHTML = "";
    openProfileButton.textContent = "Set Up Targets";
    return;
  }

  homeSummaryTitle.textContent = `${Math.round(state.profile.daily_target.calories)} kcal target`;
  homeSummaryMeta.textContent = state.profile.updated_at
    ? `Saved ${formatIso(state.profile.updated_at)}`
    : "Saved target";
  homeSummaryEmpty.hidden = true;
  homeSummaryMacros.hidden = false;
  homeSummaryMacros.innerHTML = compactMacroCards(state.profile.daily_target);
  openProfileButton.textContent = "View Profile";
}

function renderProfileSummary() {
  if (!state.profile) {
    profileSummaryTitle.textContent = "No target saved yet";
    profileMeta.textContent = "Complete the questionnaire to create one.";
    profileEmpty.hidden = false;
    profileMacros.hidden = true;
    profileMacros.innerHTML = "";
    profileEditButton.textContent = "Set Up Targets";
    return;
  }

  profileSummaryTitle.textContent = "Saved macro target";
  profileMeta.textContent = state.profile.updated_at
    ? `Updated ${formatIso(state.profile.updated_at)}`
    : "Saved target";
  profileEmpty.hidden = true;
  profileMacros.hidden = false;
  profileMacros.innerHTML = macroCards(state.profile.daily_target);
  profileEditButton.textContent = "Edit Targets";
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

function compactMacroCards(target) {
  return `
    ${compactMacroCard("Calories", `${Math.round(target.calories)} kcal`)}
    ${compactMacroCard("Protein", `${target.protein_g.toFixed(0)} g`)}
    ${compactMacroCard("Carbs", `${target.carbs_g.toFixed(0)} g`)}
    ${compactMacroCard("Fat", `${target.fat_g.toFixed(0)} g`)}
  `;
}

function compactMacroCard(label, value) {
  return `
    <article class="mini-macro-card">
      <span class="mini-macro-label">${escapeHtml(label)}</span>
      <strong class="mini-macro-value">${escapeHtml(value)}</strong>
    </article>
  `;
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
