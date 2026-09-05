const tg = window.Telegram?.WebApp ?? null;
const initData = String(tg?.initData ?? "").trim();

const HOME_VIEW = "home";
const NUTRITION_VIEW = "nutrition";
const LAB_VIEW = "nutrition-lab";
const PROFILE_VIEW = "profile";
const QUESTIONNAIRE_VIEW = "questionnaire";
const WORKOUT_VIEW = "workout";
const WORKOUT_PROGRAMME_MODE = "programme";
const WORKOUT_ACTIVE_MODE = "active";
const WORKOUT_SKIP_REASONS = [
  ["recently_trained", "Recently trained"],
  ["time_constraint", "Time constraint"],
  ["equipment_unavailable", "Equipment unavailable"],
  ["fatigue", "Fatigue"],
  ["discomfort", "Discomfort"],
  ["intentionally_skipped", "Just skip"],
  ["other", "Other"],
];
const NUTRITION_METRICS = [
  ["calories", "Calories", "kcal"],
  ["protein_g", "Protein", "g"],
  ["carbs_g", "Carbs", "g"],
  ["fat_g", "Fat", "g"],
];

const state = {
  meta: null,
  profile: null,
  nutritionDay: null,
  preview: null,
  workoutProgramme: null,
  activeWorkout: null,
  viewer: buildViewerFromTelegram(tg?.initDataUnsafe?.user ?? null),
  authMode: null,
  hasAuth: false,
  activeView: HOME_VIEW,
  workoutMode: WORKOUT_PROGRAMME_MODE,
  nutritionError: "",
  labAuthorized: null,
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
const workoutView = document.querySelector("#workout-view");
const workoutProgrammeEl = document.querySelector("#workout-programme");
const workoutSessionEl = document.querySelector("#workout-session");
const workoutCompletionDockEl = document.querySelector("#workout-completion-dock");
const workoutVersionMeta = document.querySelector("#workout-version-meta");
const pageShell = document.querySelector(".page-shell");
const appShell = document.querySelector("#app-shell");
const bottomNav = document.querySelector("#bottom-nav");
const authLoadingEl = document.querySelector("#auth-loading");
const browserLoginEl = document.querySelector("#browser-login");
const browserLoginForm = document.querySelector("#browser-login-form");
const browserLoginButton = document.querySelector("#browser-login-button");
const browserLoginError = document.querySelector("#browser-login-error");
const authErrorEl = document.querySelector("#auth-error");
const logoutButton = document.querySelector("#logout-button");

const welcomeTitle = document.querySelector("#welcome-title");
const welcomeHandle = document.querySelector("#welcome-handle");
const welcomeAvatar = document.querySelector("#welcome-avatar");
const homeSummaryTitle = document.querySelector("#home-summary-title");
const homeSummaryMeta = document.querySelector("#home-summary-meta");
const homeSummaryEmpty = document.querySelector("#home-summary-empty");
const homeSummaryMacros = document.querySelector("#home-summary-macros");
const openProfileButton = document.querySelector("#open-profile-button");
const homeDailySummaryTitle = document.querySelector("#home-daily-summary-title");
const homeDailySummaryMeta = document.querySelector("#home-daily-summary-meta");
const homeDailySummaryEmpty = document.querySelector("#home-daily-summary-empty");
const homeDailySummaryMacros = document.querySelector("#home-daily-summary-macros");

const nutritionView = document.querySelector("#nutrition-view");
const nutritionDayTitle = document.querySelector("#nutrition-day-title");
const nutritionDayMeta = document.querySelector("#nutrition-day-meta");
const nutritionDateLabel = document.querySelector("#nutrition-date-label");
const nutritionPreviousDay = document.querySelector("#nutrition-previous-day");
const nutritionNextDay = document.querySelector("#nutrition-next-day");
const nutritionProgressTitle = document.querySelector("#nutrition-progress-title");
const nutritionProgressMeta = document.querySelector("#nutrition-progress-meta");
const nutritionProgressEmpty = document.querySelector("#nutrition-progress-empty");
const nutritionProgressGrid = document.querySelector("#nutrition-progress-grid");
const nutritionMealsTitle = document.querySelector("#nutrition-meals-title");
const nutritionMealsMeta = document.querySelector("#nutrition-meals-meta");
const nutritionMealsEmpty = document.querySelector("#nutrition-meals-empty");
const nutritionMealsList = document.querySelector("#nutrition-meals-list");

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

browserLoginForm?.addEventListener("submit", handleBrowserLogin);
logoutButton?.addEventListener("click", handleBrowserLogout);

workoutProgrammeEl?.addEventListener("click", handleWorkoutClick);
workoutProgrammeEl?.addEventListener("change", handleWorkoutChange);
workoutSessionEl?.addEventListener("click", handleWorkoutClick);
workoutSessionEl?.addEventListener("change", handleWorkoutChange);
workoutSessionEl?.addEventListener("submit", handleWorkoutSubmit);
workoutCompletionDockEl?.addEventListener("click", handleWorkoutClick);
nutritionView?.addEventListener("click", handleNutritionClick);

form.addEventListener("input", () => {
  state.preview = null;
  saveButton.disabled = true;
  renderQuestionnaireContext();
  renderPreview();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.hasAuth) {
    setStatus("Sign in to preview and save targets.", "warning");
    return;
  }

  try {
    previewButton.disabled = true;
    const payload = collectAnswers();
    const response = await apiFetch("/api/targets/preview", {
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
    setStatus("Sign in to save targets.", "warning");
    return;
  }

  try {
    saveButton.disabled = true;
    const payload = collectAnswers();
    const response = await apiFetch("/api/profile", {
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
    try {
      await loadDailyNutrition(state.nutritionDay?.date || null);
    } catch (_error) {
      // Keep the saved profile success state even if the dashboard refresh fails.
    }
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

let labEnabled = false;
const labRoot = document.getElementById("nutrition-lab");
const labApi = "/api/e2e/nutrition-lab/jobs";

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
    renderNutrition();
    renderWorkoutProgramme();
  syncRoute();

  if (initData) {
    state.authMode = "telegram";
    try {
      const response = await apiFetch("/api/profile");
      revealApp("telegram", response.viewer);
      await loadAuthenticatedApp(response);
    } catch (_error) {
      showTelegramAuthError();
    }
    return;
  }

  try {
    const response = await apiFetch("/api/auth/session");
    if (!response.authenticated) {
      showBrowserLogin();
      return;
    }
    revealApp("browser", response.viewer);
    await loadAuthenticatedApp();
  } catch (_error) {
    showBrowserLogin("Could not check your browser session. Please sign in again.");
  }
}

async function loadAuthenticatedApp(profileResponse = null) {
  setStatus("Loading your saved target...", "info");
  try {
    const response = profileResponse || await apiFetch("/api/profile");
    state.meta = response;
    await initializeNutritionLab(response.capabilities?.nutrition_lab === true);
    state.profile = response.profile || null;
    state.viewer = normalizeViewer(response.viewer, state.viewer);
    renderMeta(response);
    renderViewer();
    renderHomeSummary();
    renderProfileSummary();
    renderNutrition();
    if (state.profile?.questionnaire_answers) {
      hydrateForm(state.profile.questionnaire_answers);
    }
    renderQuestionnaireContext();
    renderWorkoutProgramme();
    setStatus("", "neutral");
    try {
      await loadDailyNutrition();
    } catch (_error) {
      // The dedicated view renders its own read error; profile and workout
      // navigation remain available if the day query is temporarily down.
    }
    await loadWorkoutProgramme();
    await loadActiveWorkout();
    if (response.launch_context?.launch_type === "workout") {
      navigateTo(WORKOUT_VIEW);
    }
  } catch (error) {
    setStatus(
      error.message || "Could not load your saved target. You can still fill in the questionnaire.",
      "error"
    );
    renderQuestionnaireContext();
  }
}

function revealApp(mode, viewer = null) {
  state.authMode = mode;
  state.hasAuth = true;
  state.viewer = normalizeViewer(viewer, state.viewer);
  authLoadingEl.hidden = true;
  browserLoginEl.hidden = true;
  authErrorEl.hidden = true;
  appShell.hidden = false;
  bottomNav.hidden = false;
  logoutButton.hidden = mode !== "browser";
  renderViewer();
}

function showBrowserLogin(message = "") {
  state.authMode = null;
  state.hasAuth = false;
  authLoadingEl.hidden = true;
  authErrorEl.hidden = true;
  appShell.hidden = true;
  bottomNav.hidden = true;
  browserLoginEl.hidden = false;
  browserLoginError.textContent = message;
  browserLoginError.hidden = !message;
  if (message) {
    browserLoginForm.querySelector("input[name='username']")?.focus();
  }
}

function showTelegramAuthError() {
  state.hasAuth = false;
  authLoadingEl.hidden = true;
  browserLoginEl.hidden = true;
  appShell.hidden = true;
  bottomNav.hidden = true;
  authErrorEl.hidden = false;
}

async function handleBrowserLogin(event) {
  event.preventDefault();
  const values = new FormData(browserLoginForm);
  browserLoginError.hidden = true;
  browserLoginButton.disabled = true;
  browserLoginButton.textContent = "Signing in…";
  try {
    const response = await apiFetch("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: String(values.get("username") || ""),
        password: String(values.get("password") || ""),
      }),
    });
    browserLoginForm.reset();
    revealApp("browser", response.viewer);
    await loadAuthenticatedApp();
  } catch (error) {
    browserLoginError.textContent = error.message || "Invalid username or password.";
    browserLoginError.hidden = false;
  } finally {
    browserLoginButton.disabled = false;
    browserLoginButton.textContent = "Sign in";
  }
}

async function handleBrowserLogout() {
  logoutButton.disabled = true;
  try {
    await apiFetch("/api/auth/logout", {method: "POST"});
    clearNutritionLab();
    state.profile = null;
    state.nutritionDay = null;
    state.preview = null;
    state.workoutProgramme = null;
    state.activeWorkout = null;
    showBrowserLogin("You have been signed out.");
  } catch (error) {
    setStatus(error.message || "Could not sign out.", "error");
  } finally {
    logoutButton.disabled = false;
  }
}

async function loadWorkoutProgramme() {
  try {
    const response = await apiFetch("/api/workout/programme");
    state.workoutProgramme = response;
    renderWorkoutProgramme();
  } catch (error) {
    workoutProgrammeEl.innerHTML = `<section class="panel profile-panel"><p class="summary-empty">${escapeHtml(error.message || "Could not load the workout programme.")}</p></section>`;
  }
}

async function loadActiveWorkout() {
  try {
    const response = await apiFetch("/api/workout/sessions/active");
    state.activeWorkout = response.session || null;
    setWorkoutMode(
      state.activeWorkout ? WORKOUT_ACTIVE_MODE : WORKOUT_PROGRAMME_MODE,
      {scrollToSession: Boolean(state.activeWorkout && state.activeView === WORKOUT_VIEW)},
    );
  } catch (error) {
    if (workoutSessionEl) {
      workoutSessionEl.innerHTML = `<section class="panel profile-panel"><p class="summary-empty">${escapeHtml(error.message || "Could not load the active workout.")}</p></section>`;
    }
  }
}

async function loadDailyNutrition(targetDate = null) {
  const normalizedDate = normalizeDateKey(targetDate);
  const query = normalizedDate ? `?date=${encodeURIComponent(normalizedDate)}` : "";
  try {
    const response = await apiFetch(`/api/nutrition/day${query}`);
    state.nutritionDay = response;
    state.nutritionError = "";
    renderNutrition();
    renderHomeSummary();
    return response;
  } catch (error) {
    state.nutritionError = error.message || "Could not load your nutrition history.";
    renderNutrition();
    renderHomeSummary();
    throw error;
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
    return state.authMode === "browser" ? "Browser session connected" : "Telegram profile connected";
  }
  if (viewer.display_name) {
    return state.authMode === "browser" ? "Browser session connected" : "Telegram display name loaded";
  }
  return state.authMode === "browser"
    ? "Browser session connected"
    : "Open this Mini App from Telegram to load your identity.";
}

function viewerInitialValue(viewer) {
  const raw = viewer.display_name || viewer.username || "J";
  return raw.slice(0, 1).toUpperCase();
}

function normalizeRoute(hash = window.location.hash) {
  const route = String(hash || "").replace(/^#/, "").trim().toLowerCase();
  if (route === LAB_VIEW && state.labAuthorized !== false) return LAB_VIEW;
  if (route === NUTRITION_VIEW || route === PROFILE_VIEW || route === QUESTIONNAIRE_VIEW || route === WORKOUT_VIEW) {
    return route;
  }
  return HOME_VIEW;
}

function navigateTo(view) {
  const route = view === LAB_VIEW || view === NUTRITION_VIEW || view === QUESTIONNAIRE_VIEW || view === PROFILE_VIEW || view === WORKOUT_VIEW ? view : HOME_VIEW;
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
  const enteringWorkout = route === WORKOUT_VIEW && state.activeView !== WORKOUT_VIEW;
  state.activeView = route;
  homeView.hidden = route !== HOME_VIEW;
  nutritionView.hidden = route !== NUTRITION_VIEW;
  profileView.hidden = route !== PROFILE_VIEW;
  questionnaireView.hidden = route !== QUESTIONNAIRE_VIEW;
  workoutView.hidden = route !== WORKOUT_VIEW;
  labRoot.hidden = route !== LAB_VIEW || state.labAuthorized !== true;

  for (const button of navButtons) {
    const buttonRoute = button.dataset.route || HOME_VIEW;
    const isActive = buttonRoute === route
      || (buttonRoute === PROFILE_VIEW && route === QUESTIONNAIRE_VIEW);
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-current", isActive ? "page" : "false");
  }

  renderPreview();
  if (enteringWorkout && state.activeWorkout?.session) {
    setWorkoutMode(WORKOUT_ACTIVE_MODE, {scrollToSession: true});
  }
}

function renderWorkoutProgramme() {
  if (!workoutProgrammeEl) {
    return;
  }
  workoutProgrammeEl.hidden = state.workoutMode !== WORKOUT_PROGRAMME_MODE;
  const programme = state.workoutProgramme;
  if (!programme || !Array.isArray(programme.days)) {
    return;
  }
  const programmeVersionId = String(programme.version?.version_id || "");
  const programmeVersionNumber = programmeVersionId.match(/-v(\d+)$/)?.[1];
  workoutVersionMeta.textContent = programmeVersionId
    ? programmeVersionNumber
      ? `Programme v${programmeVersionNumber}`
      : `Programme ${programmeVersionId}`
    : "Read-only programme";
  const exercises = Object.fromEntries((programme.exercises || []).map((item) => [item.exercise_id, item]));
  const activeSession = state.activeWorkout?.session;
  const activeBanner = activeSession ? `
    <section class="panel workout-active-banner">
      <div>
        <p class="section-label">Workout in progress</p>
        <p class="workout-day-note">${escapeHtml(workoutName(activeSession))} is ready to resume.</p>
      </div>
      <button class="primary workout-start-button" type="button" data-testid="workout-resume" data-action="show-active-workout">Resume Workout</button>
    </section>` : "";
  workoutProgrammeEl.innerHTML = activeBanner + programme.days.map((day) => `
    <section class="panel workout-day-card">
      <div class="panel-head">
        <div>
          <p class="section-label">${escapeHtml(day.planned_weekday || "Planned day")}</p>
          <h3>${escapeHtml(day.display_name || day.day_code)}</h3>
        </div>
        <p>Flexible date</p>
      </div>
      <p class="workout-day-note">${escapeHtml(day.notes || "")}</p>
      ${workoutDayAction(day)}
      <ol class="workout-prescription-list">
        ${(day.prescriptions || []).map((prescription) => renderPrescription(prescription, exercises)).join("")}
      </ol>
    </section>
  `).join("");
}

function workoutDayAction(day) {
  const activeSession = state.activeWorkout?.session;
  const isActiveDay = activeSession?.programme_day_id === day.day_code;
  const action = activeSession && isActiveDay ? "resume-workout" : "start-workout";
  const label = activeSession ? (isActiveDay ? "Resume Workout" : "Workout already active") : `Start ${day.display_name || day.day_code}`;
  const disabled = activeSession && !isActiveDay ? " disabled" : "";
  return `<button class="primary workout-start-button" type="button" data-testid="workout-day-start-${escapeHtml(day.day_code)}" data-action="${action}" data-workout-day="${escapeHtml(day.day_code)}"${disabled}>${escapeHtml(label)}</button>`;
}

function renderWorkoutSession() {
  if (!workoutSessionEl) {
    return;
  }
  const active = state.activeWorkout;
  workoutSessionEl.hidden = !active?.session || state.workoutMode !== WORKOUT_ACTIVE_MODE;
  pageShell?.classList.toggle("workout-active", Boolean(active?.session));
  renderWorkoutCompletionDock(active);
  if (!active?.session) {
    workoutSessionEl.innerHTML = "";
    return;
  }
  const exercises = Object.fromEntries((state.workoutProgramme?.exercises || []).map((item) => [item.exercise_id, item]));
  const session = active.session;
  workoutSessionEl.innerHTML = `
    <section class="panel workout-session-panel" data-testid="active-workout">
      <div class="workout-session-head">
        <div>
          <p class="section-label">In progress</p>
          <h3>${escapeHtml(workoutName(session))}</h3>
        </div>
        <div class="workout-session-actions">
          <button class="ghost-button" type="button" data-action="view-programme">← View programme</button>
          <button class="ghost-button" type="button" data-action="cancel-workout">Cancel</button>
        </div>
      </div>
      <p class="workout-day-note">Started ${escapeHtml(formatIso(session.started_at))}. Your entries are saved as you go.</p>
      <div class="workout-execution-list">
        ${(active.executions || []).map((execution) => renderWorkoutExecution(execution, exercises)).join("")}
      </div>
    </section>
  `;
}

function workoutCompletionSummary(active) {
  const executions = Array.isArray(active?.executions) ? active.executions : [];
  const completed = executions.filter((execution) => {
    if (execution.status === "skipped") {
      return true;
    }
    const minimumSets = Math.max(1, Number(execution.prescribed_set_count_min) || 1);
    return resolvedWorkingSetCount(execution) >= minimumSets;
  }).length;
  return {completed, total: executions.length, ready: executions.length > 0 && completed === executions.length};
}

function isResolvedWorkingSet(set) {
  const setType = String(set?.set_type || "working").trim().toLowerCase();
  const status = String(set?.status || "completed").trim().toLowerCase();
  return setType === "working" && (status === "completed" || status === "skipped");
}

function resolvedWorkingSetCount(execution) {
  return (execution?.sets || []).filter(isResolvedWorkingSet).length;
}

function renderWorkoutCompletionDock(active) {
  if (!workoutCompletionDockEl) {
    return;
  }
  if (!active?.session) {
    workoutCompletionDockEl.hidden = true;
    workoutCompletionDockEl.innerHTML = "";
    return;
  }
  const summary = workoutCompletionSummary(active);
  workoutCompletionDockEl.hidden = false;
  workoutCompletionDockEl.innerHTML = `
    <div class="workout-completion-copy">
      <span class="section-label">Workout progress</span>
      <strong>${summary.completed} / ${summary.total} exercises completed</strong>
    </div>
    <button class="primary" type="button" data-testid="submit-workout" data-action="submit-workout"${summary.ready ? "" : " disabled"}>Submit Workout</button>
  `;
}

function workoutName(session) {
  const day = (state.workoutProgramme?.days || []).find((item) => item.day_code === session.programme_day_id);
  return day?.display_name || `${session.programme_day_id} workout`;
}

function scrollToActiveWorkout() {
  if (!workoutSessionEl || typeof window.scrollTo !== "function") {
    return;
  }
  const top = workoutSessionEl.getBoundingClientRect().top + window.scrollY - 12;
  window.scrollTo({top: Math.max(0, top), behavior: "auto"});
}

function setWorkoutMode(mode, {scrollToSession = false} = {}) {
  state.workoutMode = mode === WORKOUT_ACTIVE_MODE && state.activeWorkout?.session
    ? WORKOUT_ACTIVE_MODE
    : WORKOUT_PROGRAMME_MODE;
  renderWorkoutSession();
  renderWorkoutProgramme();
  if (scrollToSession && state.workoutMode === WORKOUT_ACTIVE_MODE) {
    const schedule = window.requestAnimationFrame || ((callback) => setTimeout(callback, 0));
    schedule(scrollToActiveWorkout);
  }
}

function renderWorkoutExecution(execution, exercises) {
  const exercise = exercises[execution.performed_exercise_id] || {};
  const options = (execution.allowed_exercise_ids || []).map((id) => exercises[id]).filter(Boolean);
  const target = execution.execution_type === "timed"
    ? `${execution.prescribed_set_count_min}–${execution.prescribed_set_count_max} sets · timed`
    : execution.prescribed_min_reps == null
      ? `${execution.prescribed_set_count_min}–${execution.prescribed_set_count_max} sets`
      : `${execution.prescribed_set_count_min}–${execution.prescribed_set_count_max} sets · ${execution.prescribed_min_reps}–${execution.prescribed_max_reps} reps`;
  const status = execution.status === "skipped" ? `<p class="workout-skipped">Skipped: ${escapeHtml(execution.skip_reason)}</p>` : "";
  const choice = options.length > 1 ? `
    <label class="workout-choice-label">Exercise
      <select data-action="choose-exercise" data-execution-id="${escapeHtml(execution.execution_id)}" data-session-id="${escapeHtml(execution.session_id)}">
        ${options.map((item) => `<option value="${escapeHtml(item.exercise_id)}"${item.exercise_id === execution.performed_exercise_id ? " selected" : ""}>${escapeHtml(item.canonical_name)}</option>`).join("")}
      </select>
    </label>` : `<p class="workout-exercise-name">${escapeHtml(exercise.canonical_name || execution.performed_exercise_id)}</p>`;
  const sets = (execution.sets || []).map((set) => `
    <div class="workout-set-row" data-testid="workout-set-row-${escapeHtml(execution.execution_id)}-${set.set_ordinal}">
      <span>${String(set.set_type || "working").toLowerCase() === "warmup" ? "Warm-up" : "Set"} ${set.set_ordinal}</span>
      <strong>${escapeHtml(formatSetResult(set))}</strong>
      <span>${set.status === "skipped" ? "Skipped" : "Saved"}</span>
    </div>`).join("");
  const nextOrdinal = Math.max(0, ...(execution.sets || []).map((item) => Number(item.set_ordinal) || 0)) + 1;
  const setForm = execution.status === "skipped" ? "" : renderSetForm(execution, nextOrdinal);
  const skipControls = execution.status === "skipped" ? "" : renderSkipControls(
    "exercise",
    `data-session-id="${escapeHtml(execution.session_id)}" data-execution-id="${escapeHtml(execution.execution_id)}" data-revision="${execution.revision}"`,
  );
  return `<article class="workout-execution-card" data-testid="workout-execution-${escapeHtml(execution.execution_id)}">
    <div class="workout-execution-head">
      <div><span class="section-label">Exercise ${execution.prescription_sequence}</span>${choice}<p class="workout-target">${escapeHtml(target)}</p></div>
      ${skipControls}
    </div>
    ${status}
    <div class="workout-set-list">${sets}</div>
    ${setForm}
  </article>`;
}

function renderSkipControls(kind, buttonAttributes) {
  const label = kind === "set" ? "Reason for skipping set" : "Reason for skipping exercise";
  const options = WORKOUT_SKIP_REASONS.map(([value, text]) => `<option value="${value}"${value === "intentionally_skipped" ? " selected" : ""}>${text}</option>`).join("");
  const buttonLabel = kind === "set" ? "Skip Set" : "Skip Exercise";
  return `<div class="workout-skip-controls" data-skip-kind="${kind}"><select data-skip-reason-select aria-label="${label}">${options}</select><button class="ghost-button" type="button" data-testid="workout-skip-${kind}" data-action="skip-${kind}" aria-label="${buttonLabel}" ${buttonAttributes}>${buttonLabel}</button></div>`;
}

function renderSetForm(execution, ordinal) {
  const prefix = `data-session-id="${escapeHtml(execution.session_id)}" data-execution-id="${escapeHtml(execution.execution_id)}" data-ordinal="${ordinal}"`;
  const previousSet = [...(execution.sets || [])]
    .filter((set) => String(set.set_type || "working").trim().toLowerCase() === "working" && String(set.status || "completed").trim().toLowerCase() === "completed")
    .sort((left, right) => (Number(left.set_ordinal) || 0) - (Number(right.set_ordinal) || 0))
    .slice(-1)[0];
  const inputValue = (value) => value == null ? "" : ` value="${escapeHtml(value)}"`;
  let fields = "";
  if (execution.execution_type === "timed") {
    fields = `<label>Seconds<input name="duration_seconds" type="number" min="1" inputmode="numeric" required${inputValue(previousSet?.duration_seconds)}></label>`;
  } else if (execution.execution_type === "side_aware_reps") {
    fields = `<label>Load (kg)<input name="load_value" type="number" min="0" step="0.25" inputmode="decimal" required${inputValue(previousSet?.load_value)}></label><label>Left reps<input name="left_reps" type="number" min="1" inputmode="numeric" required${inputValue(previousSet?.side_reps?.left)}></label><label>Right reps<input name="right_reps" type="number" min="1" inputmode="numeric" required${inputValue(previousSet?.side_reps?.right)}></label>`;
  } else {
    const load = execution.loading_convention === "per_dumbbell_kg" ? "Weight per dumbbell (kg)" : "Load (kg)";
    fields = execution.execution_type === "loaded_reps" ? `<label>${load}<input name="load_value" type="number" min="0" step="0.25" inputmode="decimal" required${inputValue(previousSet?.load_value)}></label>` : "";
    fields += `<label>Reps<input name="reps" type="number" min="1" inputmode="numeric" required${inputValue(previousSet?.reps)}></label>`;
  }
  if (execution.execution_type !== "timed") {
    fields += `<label>RIR <span class="optional-label">optional</span><input name="rir" type="number" min="0" max="10" step="0.5" inputmode="decimal"${inputValue(previousSet?.rir)}></label>`;
  }
  const skipControls = renderSkipControls(
    "set",
    `${prefix} data-execution-revision="${execution.revision}"`,
  );
  const repeatButton = previousSet ? `<button class="ghost-button workout-repeat-button" type="button" data-testid="workout-repeat-set" data-action="repeat-previous-set">Repeat previous set</button>` : "";
  return `<form class="workout-set-form" data-testid="workout-set-form-${escapeHtml(execution.execution_id)}-${ordinal}" data-set-form ${prefix} data-execution-revision="${execution.revision}"><div class="workout-set-fields">${fields}</div>${repeatButton}<div class="workout-set-actions"><button class="primary" data-testid="workout-save-set" type="submit">Save Set ${ordinal}</button>${skipControls}</div></form>`;
}

function repeatPreviousSet(formElement, execution) {
  const previousSet = [...(execution?.sets || [])]
    .filter((set) => String(set.set_type || "working").trim().toLowerCase() === "working" && String(set.status || "completed").trim().toLowerCase() === "completed")
    .sort((left, right) => (Number(left.set_ordinal) || 0) - (Number(right.set_ordinal) || 0))
    .slice(-1)[0];
  if (!previousSet) return false;
  const setValue = (name, value) => {
    const input = formElement.elements.namedItem(name);
    if (input) input.value = value == null ? "" : value;
  };
  setValue("load_value", previousSet.load_value);
  setValue("reps", previousSet.reps);
  setValue("left_reps", previousSet.side_reps?.left);
  setValue("right_reps", previousSet.side_reps?.right);
  setValue("duration_seconds", previousSet.duration_seconds);
  setValue("rir", previousSet.rir);
  return true;
}

function formatSetResult(set) {
  if (set.status === "skipped") return set.skip_reason || "Skipped";
  if (set.duration_seconds != null) return `${set.duration_seconds}s`;
  if (set.side_reps) return `${set.load_value ?? ""} kg · L ${set.side_reps.left} / R ${set.side_reps.right}`;
  if (set.load_value != null) return `${set.load_value} kg × ${set.reps}`;
  return `${set.reps} reps`;
}

async function handleWorkoutClick(event) {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  try {
    if (action === "start-workout" || action === "resume-workout") {
      const response = await apiFetch("/api/workout/sessions", {method: "POST", body: JSON.stringify({day_code: button.dataset.workoutDay})});
      state.activeWorkout = response.session;
      setWorkoutMode(WORKOUT_ACTIVE_MODE, {scrollToSession: true});
      setStatus("Workout saved. Log each set as you complete it.", "success");
      return;
    }
    if (action === "show-active-workout") {
      setWorkoutMode(WORKOUT_ACTIVE_MODE, {scrollToSession: true});
      return;
    }
    if (action === "view-programme") {
      setWorkoutMode(WORKOUT_PROGRAMME_MODE);
      return;
    }
    if (action === "repeat-previous-set") {
      const formElement = button.closest("[data-set-form]");
      const execution = state.activeWorkout?.executions.find((item) => item.execution_id === formElement?.dataset.executionId);
      if (formElement && repeatPreviousSet(formElement, execution)) {
        setStatus("Previous set values copied. Adjust them if needed, then save.", "info");
      }
      return;
    }
    if (action === "skip-exercise") {
      const reasonSelect = button.closest(".workout-skip-controls")?.querySelector("[data-skip-reason-select]");
      const response = await apiFetch(`/api/workout/sessions/${encodeURIComponent(button.dataset.sessionId)}/executions/${encodeURIComponent(button.dataset.executionId)}/skip`, {method: "POST", body: JSON.stringify({skip_reason: reasonSelect?.value || "intentionally_skipped", expected_revision: Number(button.dataset.revision)})});
      state.activeWorkout = response;
      renderWorkoutSession();
      return;
    }
    if (action === "skip-set") {
      const reasonSelect = button.closest(".workout-skip-controls")?.querySelector("[data-skip-reason-select]");
      const response = await apiFetch(`/api/workout/sessions/${encodeURIComponent(button.dataset.sessionId)}/executions/${encodeURIComponent(button.dataset.executionId)}/sets/${button.dataset.ordinal}/skip`, {method: "POST", body: JSON.stringify({skip_reason: reasonSelect?.value || "intentionally_skipped", execution_expected_revision: Number(button.dataset.executionRevision)})});
      state.activeWorkout = response;
      renderWorkoutSession();
      return;
    }
    if (action === "cancel-workout") {
      const response = await apiFetch(`/api/workout/sessions/${encodeURIComponent(state.activeWorkout.session.session_id)}/cancel`, {method: "POST", body: JSON.stringify({expected_revision: state.activeWorkout.session.revision})});
      state.activeWorkout = null;
      setWorkoutMode(WORKOUT_PROGRAMME_MODE);
      setStatus("Workout cancelled. Your saved history remains intact.", "info");
      return response;
    }
    if (action === "submit-workout") {
      button.disabled = true;
      const response = await apiFetch(`/api/workout/sessions/${encodeURIComponent(state.activeWorkout.session.session_id)}/complete`, {method: "POST", body: JSON.stringify({expected_revision: state.activeWorkout.session.revision})});
      state.activeWorkout = null;
      setWorkoutMode(WORKOUT_PROGRAMME_MODE);
      setStatus("Workout submitted. Your saved history has been updated.", "success");
      return response;
    }
  } catch (error) {
    setStatus(error.message || "Could not update the workout.", "error");
  }
}

async function handleWorkoutChange(event) {
  const select = event.target.closest('[data-action="choose-exercise"]');
  if (!select) return;
  const execution = state.activeWorkout?.executions.find((item) => item.execution_id === select.dataset.executionId);
  const allowedExerciseIds = new Set((execution?.allowed_exercise_ids || []).map((id) => String(id)));
  if (!execution || !allowedExerciseIds.has(String(select.value))) {
    setStatus("That exercise is not allowed for this prescription.", "error");
    renderWorkoutSession();
    return;
  }
  try {
    const response = await apiFetch(`/api/workout/sessions/${encodeURIComponent(select.dataset.sessionId)}/executions/${encodeURIComponent(select.dataset.executionId)}`, {method: "PUT", body: JSON.stringify({performed_exercise_id: select.value, expected_revision: Number((state.activeWorkout.executions.find((item) => item.execution_id === select.dataset.executionId) || {}).revision)})});
    state.activeWorkout = response;
    renderWorkoutSession();
  } catch (error) {
    setStatus(error.message || "Could not select that exercise.", "error");
  }
}

async function handleWorkoutSubmit(event) {
  const formElement = event.target.closest("[data-set-form]");
  if (!formElement) return;
  event.preventDefault();
  const execution = state.activeWorkout?.executions.find((item) => item.execution_id === formElement.dataset.executionId);
  if (!execution) return;
  const values = new FormData(formElement);
  const payload = {
    execution_expected_revision: Number(formElement.dataset.executionRevision),
  };
  if (values.get("load_value") !== null && values.get("load_value") !== "") payload.load_value = Number(values.get("load_value"));
  if (values.get("reps") !== null && values.get("reps") !== "") payload.reps = Number(values.get("reps"));
  const leftReps = String(values.get("left_reps") ?? "").trim();
  const rightReps = String(values.get("right_reps") ?? "").trim();
  if (execution.execution_type === "side_aware_reps") {
    const left = Number(leftReps);
    const right = Number(rightReps);
    if (!leftReps || !rightReps || !Number.isInteger(left) || !Number.isInteger(right) || left < 1 || right < 1) {
      setStatus("Enter a whole-number rep count for both left and right sides.", "error");
      return;
    }
    payload.side_reps = {left, right};
  }
  if (values.get("duration_seconds") !== null && values.get("duration_seconds") !== "") payload.duration_seconds = Number(values.get("duration_seconds"));
  if (values.get("rir") !== null && values.get("rir") !== "") payload.rir = Number(values.get("rir"));
  const existing = (execution.sets || []).find((item) => Number(item.set_ordinal) === Number(formElement.dataset.ordinal));
  if (existing) payload.expected_revision = Number(existing.revision);
  try {
    const response = await apiFetch(`/api/workout/sessions/${encodeURIComponent(formElement.dataset.sessionId)}/executions/${encodeURIComponent(formElement.dataset.executionId)}/sets/${formElement.dataset.ordinal}`, {method: "PUT", body: JSON.stringify(payload)});
    state.activeWorkout = response;
    renderWorkoutSession();
  } catch (error) {
    setStatus(error.message || "Could not save the set.", "error");
  }
}

function renderPrescription(prescription, exercises) {
  const options = (prescription.allowed_exercise_ids || []).map((id) => exercises[id]).filter(Boolean);
  const targets = prescription.option_targets || {};
  const sameTarget = options.length <= 1 || options.every((item) => JSON.stringify(targets[item.exercise_id] || {}) === JSON.stringify(targets[options[0]?.exercise_id] || {}));
  const setLabel = formatSetRange(prescription.set_min, prescription.set_max);
  const sharedTarget = formatTarget(setLabel, prescription.rep_min, prescription.rep_max, null);
  const optionsMarkup = options.map((exercise) => {
    const target = sameTarget ? sharedTarget : formatOptionTarget(prescription, exercise.exercise_id, targets[exercise.exercise_id]);
    const convention = exercise.loading_convention === "per_dumbbell_kg" ? " · kg per dumbbell" : "";
    return `<li>${escapeHtml(exercise.canonical_name)}${escapeHtml(convention)}${target ? ` — ${escapeHtml(target)}` : ""}</li>`;
  }).join("");
  return `<li class="workout-prescription">
    <div class="workout-prescription-head"><strong>${escapeHtml(prescription.display_label || "Exercise")}</strong>${prescription.optional ? " <span class=\"workout-optional\">Optional</span>" : ""}</div>
    <ul class="workout-options">${optionsMarkup || `<li>${escapeHtml((prescription.allowed_exercise_ids || []).join(" or "))}</li>`}</ul>
    ${prescription.notes ? `<p class="workout-prescription-note">${escapeHtml(prescription.notes)}</p>` : ""}
  </li>`;
}

function formatOptionTarget(prescription, exerciseId, target) {
  const selected = target || {};
  return formatTarget(
    formatSetRange(selected.set_min ?? prescription.set_min, selected.set_max ?? prescription.set_max),
    selected.rep_min,
    selected.rep_max,
    selected.execution_type === "timed" ? selected.target_note || "Timed target" : null,
  );
}

function formatTarget(setLabel, repMin, repMax, timedLabel) {
  if (timedLabel) {
    return `${setLabel} · ${timedLabel}`;
  }
  if (repMin == null || repMax == null) {
    return setLabel;
  }
  return `${setLabel} · ${repMin}–${repMax} reps`;
}

function formatSetRange(min, max) {
  if (min == null) return "Sets as configured";
  return min === max ? `${min} sets` : `${min}–${max} sets`;
}

async function handleNutritionClick(event) {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled || !state.nutritionDay) return;
  const delta = button.dataset.action === "previous-day" ? -1 : button.dataset.action === "next-day" ? 1 : 0;
  if (!delta) return;
  const nextDate = shiftDateKey(state.nutritionDay.date, delta);
  if (delta > 0 && nextDate > (normalizeDateKey(state.nutritionDay.today) || todayDateKey(state.nutritionDay.timezone))) return;
  nutritionPreviousDay.disabled = true;
  nutritionNextDay.disabled = true;
  try {
    await loadDailyNutrition(nextDate);
  } catch (error) {
    setStatus(error.message || "Could not load that day.", "error");
  } finally {
    renderNutrition();
  }
}

function renderNutrition() {
  const day = state.nutritionDay;
  const hasDay = Boolean(day && normalizeDateKey(day.date));
  const target = day?.target;

  if (!hasDay) {
    nutritionDayTitle.textContent = "Nutrition history";
    nutritionDayMeta.textContent = state.nutritionError || "Your confirmed meals for the selected day.";
    nutritionDateLabel.textContent = "Loading date…";
    nutritionProgressTitle.textContent = state.nutritionError ? "Nutrition unavailable" : "Loading your progress…";
    nutritionProgressMeta.textContent = "Target · consumed · remaining";
    nutritionProgressEmpty.textContent = state.nutritionError || "Your daily progress will appear here.";
    nutritionProgressEmpty.hidden = false;
    nutritionProgressGrid.hidden = true;
    nutritionMealsMeta.textContent = "No meals loaded yet.";
    nutritionMealsEmpty.textContent = state.nutritionError || "Your confirmed meals will appear here.";
    nutritionMealsEmpty.hidden = false;
    nutritionMealsList.hidden = true;
    nutritionPreviousDay.disabled = true;
    nutritionNextDay.disabled = true;
    renderHomeDailySummary();
    return;
  }

  const today = normalizeDateKey(day.today) || todayDateKey(day.timezone);
  nutritionDayTitle.textContent = day.date === today ? "Today" : formatNutritionDate(day.date);
  nutritionDayMeta.textContent = `${day.meal_count || 0} confirmed ${(day.meal_count || 0) === 1 ? "meal" : "meals"} · ${day.timezone || "local time"}`;
  nutritionDateLabel.textContent = formatNutritionDate(day.date);
  nutritionPreviousDay.disabled = false;
  nutritionNextDay.disabled = day.date >= today;

  if (!target) {
    nutritionProgressTitle.textContent = "No target saved yet";
    nutritionProgressMeta.textContent = "Set a target in Profile to see remaining macros.";
    nutritionProgressEmpty.textContent = "Open Profile to create your first calorie and macro target.";
    nutritionProgressEmpty.hidden = false;
    nutritionProgressGrid.hidden = true;
  } else {
    nutritionProgressTitle.textContent = `${formatNutritionAmount(target.calories, "calories")} target`;
    nutritionProgressMeta.textContent = day.target_effective_at
      ? `Effective from ${formatIso(day.target_effective_at)}`
      : "Using your saved target";
    nutritionProgressEmpty.hidden = true;
    nutritionProgressGrid.hidden = false;
    nutritionProgressGrid.innerHTML = nutritionProgressCards(day);
  }

  const meals = Array.isArray(day.meals) ? day.meals : [];
  nutritionMealsTitle.textContent = `${meals.length} confirmed ${meals.length === 1 ? "meal" : "meals"}`;
  nutritionMealsMeta.textContent = "Only confirmed Telegram meals are included.";
  nutritionMealsEmpty.textContent = "No confirmed meals for this day.";
  nutritionMealsEmpty.hidden = meals.length > 0;
  nutritionMealsList.hidden = meals.length === 0;
  nutritionMealsList.innerHTML = meals.map((meal, index) => nutritionMealCard(meal, day.timezone, index)).join("");
  renderHomeDailySummary();
}

function nutritionProgressCards(day) {
  return NUTRITION_METRICS.map(([key, label]) => {
    const consumed = day.consumed?.[key] ?? 0;
    const target = day.target?.[key] ?? 0;
    const rawRemaining = Number(day.remaining_raw?.[key] ?? day.remaining?.[key] ?? 0);
    const remaining = day.remaining?.[key] ?? Math.max(0, rawRemaining);
    const overTarget = rawRemaining < 0;
    return `<article class="nutrition-progress-card" data-testid="nutrition-progress-${key}">
      <div class="nutrition-progress-card-head"><span class="nutrition-progress-label">${label}</span><span class="nutrition-progress-unit">${key === "calories" ? "Daily total" : "Daily macro"}</span></div>
      <div class="nutrition-progress-values">
        <div><span>Consumed</span><strong>${escapeHtml(formatNutritionAmount(consumed, key))}</strong></div>
        <div><span>Target</span><strong>${escapeHtml(formatNutritionAmount(target, key))}</strong></div>
      </div>
      <p class="nutrition-progress-remaining${overTarget ? " is-over" : ""}">${escapeHtml(overTarget ? `${formatNutritionAmount(Math.abs(rawRemaining), key)} over target` : `${formatNutritionAmount(remaining, key)} remaining`)}</p>
    </article>`;
  }).join("");
}

function nutritionMealCard(meal, timezone, index) {
  const macros = meal.macros || {};
  const mealId = String(meal.meal_id || meal.eaten_at || `meal-${index}`).replace(/[^a-zA-Z0-9_-]/g, "-");
  return `<article class="nutrition-meal-card" data-testid="nutrition-meal-${escapeHtml(mealId)}">
    <div class="nutrition-meal-head">
      <div><p class="nutrition-meal-time">${escapeHtml(formatMealTime(meal.eaten_at, timezone))}</p><h4>${escapeHtml(meal.caption || "Confirmed meal")}</h4></div>
      <strong class="nutrition-meal-calories">${escapeHtml(formatNutritionAmount(macros.calories, "calories"))}</strong>
    </div>
    <div class="nutrition-meal-macros">
      <span>Protein <strong>${escapeHtml(formatNutritionAmount(macros.protein_g, "protein_g"))}</strong></span>
      <span>Carbs <strong>${escapeHtml(formatNutritionAmount(macros.carbs_g, "carbs_g"))}</strong></span>
      <span>Fat <strong>${escapeHtml(formatNutritionAmount(macros.fat_g, "fat_g"))}</strong></span>
    </div>
  </article>`;
}

function renderHomeDailySummary() {
  const day = state.nutritionDay;
  if (!day || !day.target) {
    homeDailySummaryTitle.textContent = state.nutritionError ? "Nutrition unavailable" : "Nutrition loading…";
    homeDailySummaryMeta.textContent = state.nutritionError || "Open Nutrition for the full day view.";
    homeDailySummaryEmpty.textContent = state.nutritionError || "Your confirmed meals and daily progress will appear here.";
    homeDailySummaryEmpty.hidden = false;
    homeDailySummaryMacros.hidden = true;
    homeDailySummaryMacros.innerHTML = "";
    return;
  }

  homeDailySummaryTitle.textContent = `${formatNutritionAmount(day.consumed?.calories, "calories")} consumed`;
  homeDailySummaryMeta.textContent = `${formatNutritionAmount(day.target.calories, "calories")} target · ${day.meal_count || 0} confirmed ${(day.meal_count || 0) === 1 ? "meal" : "meals"}`;
  homeDailySummaryEmpty.hidden = true;
  homeDailySummaryMacros.hidden = false;
  homeDailySummaryMacros.innerHTML = NUTRITION_METRICS.map(([key, label]) => (
    compactMacroCard(label, `${formatNutritionAmount(day.consumed?.[key], key)} / ${formatNutritionAmount(day.target?.[key], key)}`)
  )).join("");
}

function formatNutritionAmount(value, key) {
  const amount = Number(value || 0);
  if (key === "calories") return `${Math.round(amount)} kcal`;
  return `${amount.toFixed(1)} g`;
}

function formatMealTime(value, timezone) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "Time unavailable";
  try {
    return date.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit", timeZone: timezone || "UTC"});
  } catch (_error) {
    return formatIso(value);
  }
}

function formatNutritionDate(value) {
  const date = new Date(`${normalizeDateKey(value)}T12:00:00Z`);
  if (Number.isNaN(date.getTime())) return value || "Date unavailable";
  return date.toLocaleDateString(undefined, {weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "UTC"});
}

function normalizeDateKey(value) {
  const match = String(value || "").trim().match(/^\d{4}-\d{2}-\d{2}$/);
  return match ? match[0] : "";
}

function shiftDateKey(value, delta) {
  const dateKey = normalizeDateKey(value);
  if (!dateKey) return "";
  const [year, month, day] = dateKey.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, month - 1, day + delta));
  return shifted.toISOString().slice(0, 10);
}

function todayDateKey(timezone) {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {timeZone: timezone || "UTC", year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(new Date());
    const values = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  } catch (_error) {
    return new Date().toISOString().slice(0, 10);
  }
}

function renderViewer() {
  const viewer = normalizeViewer(state.viewer);
  const primary = viewerPrimaryLabel(viewer);
  const secondary = viewerSecondaryLabel(viewer);

  welcomeTitle.textContent = primary === "there" ? "Hello there" : `Hello ${primary}`;
  welcomeHandle.textContent = secondary;
  welcomeAvatar.textContent = viewerInitialValue(viewer);

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
    renderHomeDailySummary();
    return;
  }

  homeSummaryTitle.textContent = `${Math.round(state.profile.daily_target.calories)} kcal target`;
  homeSummaryMeta.textContent = state.profile.target_effective_at
    ? `Effective from ${formatIso(state.profile.target_effective_at)}`
    : "Effective date unavailable";
  homeSummaryEmpty.hidden = true;
  homeSummaryMacros.hidden = false;
  homeSummaryMacros.innerHTML = compactMacroCards(state.profile.daily_target);
  openProfileButton.textContent = "View Profile";
  renderHomeDailySummary();
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
  profileMeta.textContent = state.profile.target_effective_at
    ? `Effective from ${formatIso(state.profile.target_effective_at)}`
    : "Effective date unavailable";
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
    setQuestionnaireNote("Sign in to preview and save your target.", "warning");
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
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  if (initData) {
    headers["X-Telegram-Init-Data"] = initData;
  }
  const response = await fetch(url, {
    method: options.method || "GET",
    headers,
    credentials: "same-origin",
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


// Dev-only evaluation UI. The server independently gates every Lab request.
let labTimer = null;
let labPreviewUrl = null;
let labRequestId = null;
let labCurrentJob = null;


function clearNutritionLab() {
  labEnabled = false;
  clearTimeout(labTimer);
  if (labPreviewUrl) URL.revokeObjectURL(labPreviewUrl);
  labPreviewUrl = null;
  labRequestId = null;
  labCurrentJob = null;
  labRoot.replaceChildren();
  labRoot.hidden = true;
}

async function initializeNutritionLab(allowed) {
  clearNutritionLab();
  state.labAuthorized = allowed && state.authMode === "browser";
  if (!state.labAuthorized) { syncRoute(); return; }
  labEnabled = true;
  labRoot.innerHTML = `
    <p class="eyebrow">Development · javaan-e2e only</p>
    <h2>E2E Nutrition Lab</h2>
    <p>Evaluate real meal photos with the production nutrition pipeline.</p>
    <form id="lab-upload-form" data-testid="lab-upload-form">
      <label>Meal image (JPEG, PNG or WebP, up to 3 MB)
        <input id="lab-image" type="file" accept="image/jpeg,image/png,image/webp" required data-testid="nutrition-lab-file">
      </label>
      <img id="lab-preview" alt="Selected meal photo" hidden>
      <label>Optional caption<textarea id="lab-caption" maxlength="1000" rows="2" data-testid="nutrition-lab-caption"></textarea></label>
      <label>Mode<select id="lab-mode" data-testid="nutrition-lab-mode">
        <option value="estimate" data-testid="nutrition-lab-mode-estimate">Estimate-only · no meal or action</option>
        <option value="log" data-testid="nutrition-lab-mode-full">Full synthetic log · correction and confirmation</option>
      </select></label>
      <label>Optional meal time (saved profile timezone)<input id="lab-eaten-at" type="datetime-local" data-testid="nutrition-lab-eaten-at"></label>
      <p>Full logs use the existing confirmation timeout and may log automatically after it expires. All writes belong to the isolated test account.</p>
      <button type="submit" id="lab-submit" data-testid="nutrition-lab-run">Analyze image</button>
    </form>
    <p id="lab-status" role="status" aria-live="polite" data-testid="lab-status"></p>
    <label>Recent runs (24 hours)<select id="lab-recent" data-testid="lab-recent"><option value="">Select a run</option></select></label>
    <div id="lab-result" data-testid="nutrition-lab-result"></div>`;
  document.getElementById("lab-upload-form").addEventListener("submit", submitLabImage);
  document.getElementById("lab-upload-form").addEventListener("input", () => { labRequestId = null; });
  document.getElementById("lab-image").addEventListener("change", event => {
    if (labPreviewUrl) URL.revokeObjectURL(labPreviewUrl);
    const file = event.target.files[0];
    labPreviewUrl = file ? URL.createObjectURL(file) : null;
    const preview = document.getElementById("lab-preview");
    preview.hidden = !labPreviewUrl;
    if (labPreviewUrl) preview.src = labPreviewUrl;
    else preview.removeAttribute("src");
  });
  document.getElementById("lab-recent").addEventListener("change", async event => {
    if (!event.target.value) return;
    clearTimeout(labTimer);
    try { renderLabJob(await apiFetch(`${labApi}/${event.target.value}`)); }
    catch (error) { labStatus(error.message); }
  });
  try {
    const {jobs} = await apiFetch(labApi);
    if (!labEnabled) return;
    for (const job of jobs) addLabRecent(job);
    if (jobs.length) renderLabJob(jobs[0]);
  } catch (error) { labStatus(error.message); }
  syncRoute();
}

function labStatus(message) {
  const element = document.getElementById("lab-status");
  if (element) element.textContent = message;
}

function addLabRecent(job) {
  const select = document.getElementById("lab-recent");
  if (!select || [...select.options].some(option => option.value === job.job_id)) return;
  const option = document.createElement("option");
  option.value = job.job_id;
  option.textContent = `${new Date(job.created_at * 1000).toLocaleTimeString()} · ${job.mode} · ${job.caption || "No caption"}`;
  select.append(option);
}

async function submitLabImage(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const file = document.getElementById("lab-image").files[0];
  if (!file || file.size > 3000000) { labStatus("Choose an image of at most 3 MB."); return; }
  const caption = document.getElementById("lab-caption").value;
  const mode = document.getElementById("lab-mode").value;
  labRequestId ||= crypto.randomUUID().replaceAll("-", "");
  const requestId = labRequestId;
  clearTimeout(labTimer);
  for (const control of form.elements) control.disabled = true;
  labStatus("Uploading real image…");
  try {
    const imageBase64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1]);
      reader.onerror = () => reject(new Error("Could not read the image."));
      reader.readAsDataURL(file);
    });
    const job = await apiFetch(`${labApi}/${requestId}`, {method: "PUT", body: JSON.stringify({image_base64: imageBase64, caption, mode, eaten_at: mode === "log" ? document.getElementById("lab-eaten-at").value || null : null})});
    if (!labEnabled) return;
    labRequestId = null;
    addLabRecent(job);
    renderLabJob(job);
  } catch (error) { labStatus(error.message + " You can retry this upload safely."); }
  finally { for (const control of form.elements) control.disabled = false; }
}

function renderLabJob(job) {
  if (!labEnabled) return;
  clearTimeout(labTimer);
  labCurrentJob = job;
  document.getElementById("lab-recent").value = job.job_id;
  const status = job.action?.status || job.status;
  labStatus(job.error || `${job.mode === "estimate" ? "Estimate-only" : "Synthetic log"}: ${status}${job.recommendation_status ? ` · Recommendation: ${job.recommendation_status}` : ""}`);
  const result = document.getElementById("lab-result");
  result.replaceChildren();
  if (job.action) {
    const message = document.createElement("pre");
    message.textContent = job.action.message;
    const previewTitle = document.createElement("h3");
    previewTitle.textContent = "Telegram Preview";
    result.append(previewTitle);
    // The production formatter owns this preview.
    message.dataset.testid = "nutrition-lab-telegram-preview";
    result.append(message);
    if (job.action.status === "pending") {
      const controls = document.createElement("div");
      controls.className = "lab-actions";
      const adjust = document.createElement("button");
      adjust.type = "button";
      adjust.textContent = "Adjust";
      adjust.dataset.testid = "nutrition-lab-adjust";
      const corrections = document.createElement("div");
      corrections.className = "lab-actions";
      corrections.hidden = true;
      adjust.addEventListener("click", () => { corrections.hidden = !corrections.hidden; });
      controls.append(adjust);
      for (const correction of job.corrections || []) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = correction.label;
        button.dataset.testid = `lab-correct-${correction.type}-${correction.value}`;
        button.addEventListener("click", () => mutateLabJob("correct", {type: correction.type, value: correction.value}));
        corrections.append(button);
      }
      controls.append(corrections);
      for (const operation of ["confirm", "cancel"]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = operation === "confirm" ? "Confirm synthetic meal" : "Cancel synthetic meal";
        button.dataset.testid = `nutrition-lab-${operation}`;
        button.addEventListener("click", () => mutateLabJob(operation));
        controls.append(button);
      }
      result.append(controls);
    }
  }
  if (job.estimate || job.action) {
    const estimate = job.action?.estimate || job.estimate;
    const addText = (heading, content, testid) => {
      const section = document.createElement("section");
      const title = document.createElement("h3");
      title.textContent = heading;
      const text = document.createElement("pre");
      if (testid) text.dataset.testid = testid;
      text.textContent = content;
      section.append(title, text);
      result.append(section);
    };
    const total = estimate.total_best || estimate;
    addText("Estimate", `${estimate.meal_name}\n${job.estimator_version} / ${job.model}\n${total.calories} kcal · P ${total.protein_g}g · C ${total.carbs_g}g · F ${total.fat_g}g\nRange: ${estimate.total_low?.calories ?? "unknown"}–${estimate.total_high?.calories ?? "unknown"} kcal\nReconciliation: ${estimate.reconciliation_status}\nFollow-up: ${estimate.follow_up_question || "None"}\nAnalysis time: ${job.latency_ms ?? "unknown"} ms`);
    addText("Items", (estimate.items || []).map(item => `${item.name}: ~${item.portion_g}g (${item.portion_low_g ?? "?"}–${item.portion_high_g ?? "?"}g) · ${item.calories} kcal · ${item.evidence}\n${item.assumptions || ""}`).join("\n\n"));
    addText("Assumptions and confidence", JSON.stringify({assumptions: (estimate.items || []).map(item => ({name: item.name, categories: item.assumption_categories})), identification: estimate.identification_confidence, portion: estimate.portion_confidence, macros: estimate.macro_confidence}, null, 2));
    if (!job.action) addText("Telegram Preview", job.telegram_preview || "Unavailable", "nutrition-lab-telegram-preview");
    if (job.action?.status === "confirmed") {
      addText("Confirmed meal", `${job.action.meal_id} · confirmed`);
      if (job.daily_state) addText("Current daily totals", JSON.stringify(job.daily_state.consumed, null, 2), "nutrition-lab-daily-totals");
    }
    if (job.recommendation || job.recommendation_status) {
      addText("Recommendation", JSON.stringify(job.recommendation || {status: job.recommendation_status}, null, 2), "nutrition-lab-recommendation");
      if (job.recommendation_telegram_preview) addText("Recommendation Telegram Preview", job.recommendation_telegram_preview, "nutrition-lab-recommendation-preview");
    }
    const title = document.createElement("h3");
    title.textContent = "Structured result";
    const pre = document.createElement("pre");
    pre.dataset.testid = "lab-json";
    pre.textContent = JSON.stringify(job, null, 2);
    const download = document.createElement("button");
    download.type = "button";
    download.textContent = "Download result JSON";
    download.addEventListener("click", () => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(job, null, 2)], {type: "application/json"}));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `nutrition-lab-${job.job_id}.json`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    result.append(title, download, pre);
  }
  if (["queued", "running"].includes(job.status) || ["queued", "running"].includes(job.recommendation_status)) {
    labTimer = setTimeout(async () => {
      try { const refreshed = await apiFetch(`${labApi}/${job.job_id}`); if (labCurrentJob?.job_id === job.job_id) renderLabJob(refreshed); }
      catch (error) { labStatus(error.message + " Reload to resume this run."); }
    }, 2000);
  }
}

async function mutateLabJob(operation, payload = {}) {
  const jobId = labCurrentJob?.job_id;
  if (!jobId) return;
  for (const button of document.querySelectorAll(".lab-actions button")) button.disabled = true;
  try {
    const job = await apiFetch(`${labApi}/${jobId}/${operation}`, {method: "POST", body: JSON.stringify(payload)});
    if (labCurrentJob?.job_id === jobId) renderLabJob(job);
    await loadDailyNutrition();
  } catch (error) {
    // A correction is deliberately never replayed automatically after a lost response.
    try { renderLabJob(await apiFetch(`${labApi}/${jobId}`)); } catch (_error) { /* reload can recover */ }
    labStatus(error.message + " Review the current result before trying again.");
  }
}
