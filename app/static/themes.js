(() => {
  'use strict';

  const STORAGE_KEY = 'k8s-universal-theme';
  const THEMES = window.K8S_THEME_CONFIG;
  if (!THEMES) return;

  const normalizeTheme = (value) => (
    typeof value === 'string' && Object.prototype.hasOwnProperty.call(THEMES, value)
      ? value
      : 'standard'
  );

  const readStoredTheme = () => {
    try {
      return normalizeTheme(localStorage.getItem(STORAGE_KEY));
    } catch (_) {
      return normalizeTheme(document.documentElement.dataset.theme);
    }
  };

  const storeTheme = (themeName) => {
    try {
      localStorage.setItem(STORAGE_KEY, themeName);
      return true;
    } catch (_) {
      return false;
    }
  };

  const updateThemeControls = (themeName, announce, persisted = true) => {
    const selectedTheme = THEMES[themeName];
    document.querySelectorAll('.theme-card[data-theme-option]').forEach((control) => {
      const isActive = control.dataset.themeOption === themeName;
      control.classList.toggle('is-active', isActive);
      control.setAttribute('aria-pressed', String(isActive));
    });
    document.querySelectorAll('.theme-current-name').forEach((label) => {
      label.textContent = selectedTheme.label;
    });
    if (announce) {
      const liveStatus = document.querySelector('[data-theme-live]');
      if (liveStatus) {
        liveStatus.textContent = persisted
          ? `${selectedTheme.label} ist jetzt aktiv und in diesem Browser gespeichert.`
          : `${selectedTheme.label} ist jetzt aktiv. Der Browser hat das Speichern verhindert.`;
      }
    }
  };

  const applyTheme = (themeName, { persist = false, announce = false } = {}) => {
    const normalizedTheme = normalizeTheme(themeName);
    const theme = THEMES[normalizedTheme];
    const root = document.documentElement;
    root.dataset.theme = normalizedTheme;
    root.style.colorScheme = theme.colorScheme;

    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) {
      themeColor.setAttribute('content', theme.themeColor);
    }

    const persisted = persist ? storeTheme(normalizedTheme) : true;
    updateThemeControls(normalizedTheme, announce, persisted);
    window.dispatchEvent(new CustomEvent('k8s-theme-change', {
      detail: { theme: normalizedTheme, persisted }
    }));
  };

  const initializeThemeControls = () => {
    applyTheme(readStoredTheme());
    document.querySelectorAll('.theme-card[data-theme-option]').forEach((control) => {
      control.addEventListener('click', () => {
        applyTheme(control.dataset.themeOption, { persist: true, announce: true });
      });
    });
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeThemeControls, { once: true });
  } else {
    initializeThemeControls();
  }

  window.addEventListener('storage', (event) => {
    if (event.key === STORAGE_KEY || event.key === null) {
      applyTheme(event.key === null ? 'standard' : event.newValue, { announce: true });
    }
  });
})();
