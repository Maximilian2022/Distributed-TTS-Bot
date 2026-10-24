// TTS Bot WebUI JavaScript

let currentVoiceSettings = null;
let currentSelectedSpeakerId = null;
let currentUserName = "";
let availableVoices = [];
let previewAudio = new Audio();
let logInterval = null;

let userGuilds = [];
let currentGuildId = null;
let currentGuildPerms = { is_admin: false, has_role: false };
let guildRoles = [];

document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  updateVolunteerOneliner();

  // ログイン済みの場合にデータを初期ロード
  if (window.APP_IS_LOGGED_IN) {
    updatePermissionsUI();
    loadUserProfile();
    loadVoices();
    loadGuilds();
    loadCharacterTerms();
    loadStats();
    loadCredentials();
  }
  renderCharacterTermsTable();
});

// --- トースト通知 ---
function showToast(message, type = "success") {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  
  const iconSpan = document.createElement("span");
  iconSpan.textContent = type === 'success' ? '✅' : '❌';
  
  const msgSpan = document.createElement("span");
  msgSpan.textContent = message || '';
  
  toast.appendChild(iconSpan);
  toast.appendChild(msgSpan);
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// --- タブ切り替え ---
function setupTabs() {
  const tabButtons = document.querySelectorAll(".tab-btn");
  const tabPanes = document.querySelectorAll(".tab-pane");

  tabButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      const tabId = btn.dataset.tab;

      // 権限チェック
      const hasAccess = currentGuildPerms.has_role || currentGuildPerms.is_admin || (window.APP_USER_DATA && window.APP_USER_DATA.is_admin);
      if ((tabId === "config" || tabId === "system" || tabId === "member-voices" || tabId === "bots" || tabId === "credentials") && !hasAccess) {
        showToast("このタブを閲覧する権限がありません（管理者または指定ロールのみ）", "error");
        return;
      }

      tabButtons.forEach(b => b.classList.remove("active"));
      tabPanes.forEach(p => p.classList.remove("active"));

      btn.classList.add("active");
      const targetPane = document.getElementById(`tab-${tabId}`);
      if (targetPane) targetPane.classList.add("active");

      // リロード時に元のタブへ戻れるようローカルストレージとURLハッシュに保存
      try {
        localStorage.setItem("ttsbot_active_tab", tabId);
        if (history.replaceState) {
          history.replaceState(null, null, '#' + tabId);
        }
      } catch (e) {}

      // タブごとのロード
      if (tabId === "bots") {
        if (typeof loadManagerNodes === "function") loadManagerNodes();
        loadBotSlots();
        if (typeof loadWorkerNodes === "function") loadWorkerNodes();
      }
      if (tabId === "credentials") {
        loadCredentials();
      }
      if (tabId === "system") {
        loadStats(currentGuildId);
        if (currentGuildId) {
          loadCacheExplorerFiles();
          loadCacheStatsSummary();
          checkModelsStatus();
          loadMyCoeiroinkList(1);
        }
        startSystemMetricsPolling();
        loadLogs();
        loadWorkerLogs(false);
      } else {
        stopSystemMetricsPolling();
        if (typeof workerLogInterval !== "undefined" && workerLogInterval) {
          clearInterval(workerLogInterval);
          workerLogInterval = null;
          const chk = document.getElementById("worker-log-auto-refresh");
          if (chk) chk.checked = false;
        }
      }
      if (tabId === "ng") {
        if (currentGuildId) loadNgWords(currentGuildId);
      }
      if (tabId === "config") {
        if (currentGuildId) loadBotConfig(currentGuildId);
      }
      if (tabId === "member-voices") {
        if (currentGuildId) loadMemberVoices(currentGuildId);
      }
      if (tabId === "volunteer") {
        updateVolunteerOneliner();
      }
      if (tabId === "help") {
        renderCharacterTermsTable();
      }
    });
  });

  // 初回ロード時に保存されたタブを復元 (loadGuilds完了後に実行するためここでは呼ばない)
}

function restoreActiveTab() {
  const hash = window.location.hash ? window.location.hash.replace('#', '') : null;
  const savedTab = hash || localStorage.getItem("ttsbot_active_tab");
  if (savedTab && savedTab !== "voice") {
    const hasAccess = currentGuildPerms.has_role || currentGuildPerms.is_admin;
    // 権限が必要なタブで未所持の場合はトーストエラーを出さずに初期タブ (voice) のまま維持
    if ((savedTab === "config" || savedTab === "system" || savedTab === "member-voices") && !hasAccess) {
      return;
    }
    const btn = document.querySelector(`.tab-btn[data-tab='${savedTab}']`);
    if (btn) {
      btn.click();
    }
  }
}

// --- サーバー（ギルド）管理 ---

async function loadGuilds() {
  try {
    const res = await fetch("/api/guilds");
    if (res.status === 401) {
      window.location.reload();
      return;
    }
    const data = await res.json();
    userGuilds = data.guilds || [];

    const selectEl = document.getElementById("guild-select");
    const container = document.getElementById("guild-selector-area");

    if (!selectEl || userGuilds.length === 0) {
      if (container) container.style.display = "none";
      return;
    }

    selectEl.innerHTML = "";
    userGuilds.forEach(g => {
      const opt = document.createElement("option");
      opt.value = g.id;
      opt.textContent = `${g.name} ${g.is_admin ? '👑' : ''}`;
      selectEl.appendChild(opt);
    });

    if (container) container.style.display = "flex";

    // 保存されている前回のギルドIDまたは先頭を採用
    const savedGuildId = localStorage.getItem("ttsbot_selected_guild");
    const targetGuild = userGuilds.find(g => g.id === savedGuildId) || userGuilds[0];

    selectEl.value = targetGuild.id;
    await switchGuild(targetGuild.id);
    restoreActiveTab();
  } catch (e) {
    console.error("Failed to load guilds:", e);
  }
}

async function onGuildChanged(guildId) {
  if (guildId) {
    await switchGuild(guildId);
  }
}

async function switchGuild(guildId) {
  currentGuildId = guildId;
  localStorage.setItem("ttsbot_selected_guild", guildId);

  const guild = userGuilds.find(g => g.id === guildId);
  if (guild) {
    currentGuildPerms = {
      is_admin: !!guild.is_admin,
      has_role: !!guild.has_role
    };
  }

  // 権限に応じたUI表示更新
  updatePermissionsUI();

  // サーバー別データの読み込み
  loadDictionary(currentGuildId);
  loadNgWords(currentGuildId);
  loadUserProfile(currentGuildId);
  loadSyncSourceGuilds();
  await Promise.all([
    loadGuildRoles(currentGuildId),
    loadGuildChannels(currentGuildId)
  ]);

  const hasAccess = currentGuildPerms.has_role || currentGuildPerms.is_admin;
  if (hasAccess) {
    await loadBotConfig(currentGuildId);
    loadStats(currentGuildId);
    loadCacheExplorerFiles();
    loadCacheStatsSummary();
    checkModelsStatus();
    loadMemberVoices(currentGuildId);
    const activeTab = document.querySelector(".tab-btn.active");
    if (activeTab && activeTab.dataset.tab === "system") {
      startSystemMetricsPolling();
    }
  }
}

function updatePermissionsUI() {
  const btnConfig = document.getElementById("tab-btn-config");
  const btnSystem = document.getElementById("tab-btn-system");
  const btnMemberVoices = document.getElementById("tab-btn-member-voices");
  const cardConfigForbidden = document.getElementById("config-forbidden-card");
  const cardConfigForm = document.getElementById("config-form-card");
  const cardConfigStandby = document.getElementById("config-standby-card");
  const cardSystemForbidden = document.getElementById("system-forbidden-card");
  const areaSystemContent = document.getElementById("system-content-area");
  const cardMemberVoicesForbidden = document.getElementById("member-voices-forbidden-card");
  const areaMemberVoicesContent = document.getElementById("member-voices-content-area");

  const btnBots = document.getElementById("tab-btn-bots");
  const btnCreds = document.getElementById("tab-btn-credentials");

  const hasAccess = currentGuildPerms.has_role || currentGuildPerms.is_admin || (window.APP_USER_DATA && window.APP_USER_DATA.is_admin);

  if (btnBots) {
    btnBots.style.display = hasAccess ? "inline-block" : "none";
  }
  if (btnCreds) {
    btnCreds.style.display = hasAccess ? "inline-block" : "none";
  }

  if (btnMemberVoices) {
    btnMemberVoices.style.display = hasAccess ? "inline-block" : "none";
  }
  if (cardMemberVoicesForbidden && areaMemberVoicesContent) {
    cardMemberVoicesForbidden.style.display = hasAccess ? "none" : "block";
    areaMemberVoicesContent.style.display = hasAccess ? "block" : "none";
  }

  if (btnConfig) {
    btnConfig.style.display = hasAccess ? "inline-block" : "none";
  }
  if (btnSystem) {
    btnSystem.style.display = hasAccess ? "inline-block" : "none";
  }

  if (cardConfigForbidden && cardConfigForm) {
    cardConfigForbidden.style.display = hasAccess ? "none" : "block";
    cardConfigForm.style.display = hasAccess ? "block" : "none";
  }

  if (cardConfigStandby) {
    cardConfigStandby.style.display = hasAccess ? "block" : "none";
  }
  if (hasAccess) {
    updateStandbyOneliner();
  }

  if (cardSystemForbidden && areaSystemContent) {
    cardSystemForbidden.style.display = hasAccess ? "none" : "block";
    areaSystemContent.style.display = hasAccess ? "block" : "none";
  }

  // 管理者専用コントロール
  const adminBadge = document.getElementById("role-admin-badge");
  const roleSelect = document.getElementById("cfg-admin-role");
  if (adminBadge) adminBadge.style.display = currentGuildPerms.is_admin ? "inline-block" : "none";
  if (roleSelect) roleSelect.disabled = !currentGuildPerms.is_admin;

  // 権限がないのにアクセス制限タブを開いている場合はマイ音声タブに遷移
  const activeTabBtn = document.querySelector(".tab-btn.active");
  if (activeTabBtn) {
    const tabName = activeTabBtn.dataset.tab;
    if ((tabName === "config" || tabName === "system" || tabName === "member-voices" || tabName === "bots" || tabName === "credentials") && !hasAccess) {
      const voiceBtn = document.querySelector(".tab-btn[data-tab='voice']");
      if (voiceBtn) voiceBtn.click();
    }
  }
}

// --- ユーザー設定と話者一覧の読み込み ---

async function loadUserProfile(guildId = null) {
  try {
    const targetGid = guildId || currentGuildId;
    const url = targetGid ? `/api/me?guild_id=${targetGid}` : "/api/me";
    const res = await fetch(url);
    if (res.status === 401) {
      window.location.reload();
      return;
    }
    const data = await res.json();
    currentVoiceSettings = data.voice;
    currentSelectedSpeakerId = currentVoiceSettings ? currentVoiceSettings.speaker_id : "nanami";
    currentUserName = data.user.global_name || data.user.username;

    // ヘッダーのユーザー情報更新
    document.getElementById("user-display-name").textContent = currentUserName;
    if (data.user.avatar_url) {
      document.getElementById("user-avatar-img").src = data.user.avatar_url;
    }

    // 同期バナーの対象サーバー名バッジを更新
    const syncBadge = document.getElementById("sync-target-guild-badge");
    if (syncBadge) {
      const currentG = userGuilds.find(g => g.id === targetGid);
      syncBadge.textContent = `対象: ${currentG ? currentG.name : '全サーバー共通'}`;
    }

    // スライダー等の初期化
    initVoiceControls(currentVoiceSettings);
  } catch (e) {
    console.error("Failed to load user profile:", e);
  }
}

let currentProviderFilter = 'all';
let currentVoiceSearchQuery = '';

async function loadVoices() {
  try {
    const res = await fetch("/api/voices");
    const data = await res.json();
    availableVoices = data.voices || [];
    renderVoiceChips();
    if (currentVoiceSettings) {
      initVoiceControls(currentVoiceSettings);
    } else if (availableVoices.length > 0 && !currentSelectedSpeakerId) {
      selectVoiceDetail(availableVoices[0].id, false);
    }
  } catch (e) {
    console.error("Failed to load voices:", e);
  }
}

let currentGenderFilter = "all";
let currentDetailPitch = 0;
let currentDetailSpeed = 1.0;
let currentDetailVolume = 80;
let currentDetailStrip = true;

function filterVoiceGender(gender) {
  currentGenderFilter = gender;
  document.querySelectorAll(".gender-tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.gender === gender);
  });
  renderVoiceChips();
}

function filterVoiceProvider(provider) {
  currentProviderFilter = provider;
  document.querySelectorAll(".voice-tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.provider === provider);
  });
  renderVoiceChips();
}

function onVoiceSearchInput(query) {
  currentVoiceSearchQuery = (query || "").trim().toLowerCase();
  renderVoiceChips();
}

function renderVoiceChips() {
  const container = document.getElementById("speakers-container");
  if (!container) return;
  container.innerHTML = "";

  // プロバイダー別件数カウント
  const counts = { all: availableVoices.length, azure: 0, google: 0, voicevox: 0, sharevox: 0, coeiroink: 0, edge: 0, openjtalk: 0 };
  availableVoices.forEach(v => {
    if (counts[v.provider] !== undefined) counts[v.provider]++;
  });

  // タブボタンのカウント更新
  document.querySelectorAll(".voice-tab-btn").forEach(btn => {
    const p = btn.dataset.provider;
    if (p === 'all') btn.textContent = `すべて (${counts.all})`;
    if (p === 'edge') btn.textContent = `Edge (${counts.edge})`;
    if (p === 'azure') btn.textContent = `Azure (${counts.azure})`;
    if (p === 'google') btn.textContent = `Google (${counts.google})`;
    if (p === 'voicevox') btn.textContent = `VOICEVOX (${counts.voicevox})`;
    if (p === 'sharevox') btn.textContent = `SHAREVOX (${counts.sharevox})`;
    if (p === 'coeiroink') btn.textContent = `COEIROINK (${counts.coeiroink})`;
    if (p === 'openjtalk') btn.textContent = `OpenJTalk (${counts.openjtalk || 0})`;
  });

  // フィルタリング処理 (プロバイダー x 性別 x 検索キーワード)
  const filtered = availableVoices.filter(voice => {
    // 1. プロバイダーフィルター
    if (currentProviderFilter !== 'all' && voice.provider !== currentProviderFilter) {
      return false;
    }
    // 2. 性別フィルター
    if (currentGenderFilter !== 'all') {
      const g = voice.gender || 'neutral';
      if (currentGenderFilter === 'female' && g !== 'female') return false;
      if (currentGenderFilter === 'male' && g !== 'male') return false;
      if (currentGenderFilter === 'neutral' && (g === 'female' || g === 'male')) return false;
    }
    // 3. 検索キーワードフィルター
    if (currentVoiceSearchQuery) {
      const matchName = (voice.name || "").toLowerCase().includes(currentVoiceSearchQuery);
      const matchDesc = (voice.description || "").toLowerCase().includes(currentVoiceSearchQuery);
      const matchId = (voice.id || "").toLowerCase().includes(currentVoiceSearchQuery);
      if (!matchName && !matchDesc && !matchId) {
        return false;
      }
    }
    return true;
  });

  // 件数バッジ更新
  const countBadge = document.getElementById("voice-count-badge");
  if (countBadge) {
    countBadge.textContent = `${filtered.length} / 全${availableVoices.length}モデル`;
  }

  if (filtered.length === 0) {
    container.innerHTML = `
      <div style="grid-column: 1 / -1; text-align: center; padding: 32px 16px; color: var(--text-dim); font-size: 13px;">
        🔍 条件に一致するモデルが見つかりませんでした。<br>
        <span style="font-size:12px;">別のキーワードで検索するか、フィルター条件を切り替えてください。</span>
      </div>
    `;
    return;
  }

  const savedSpeakerId = currentVoiceSettings ? currentVoiceSettings.speaker_id : "nanami";
  if (!currentSelectedSpeakerId) {
    currentSelectedSpeakerId = savedSpeakerId;
  }

  filtered.forEach(voice => {
    const isSaved = (voice.id === savedSpeakerId);
    const isSelected = (voice.id === currentSelectedSpeakerId);
    
    // 性別アイコン
    const genderIcon = voice.gender === 'female' ? '👧' : (voice.gender === 'male' ? '👦' : '🤖');

    // スタイル名とキャラ名の分離（例: "四国めたん (ノーマル)" または "四国めたん"）
    let displayName = voice.name || voice.id;
    let styleText = "";
    if (displayName.includes("(") && displayName.endsWith(")")) {
      const idx = displayName.lastIndexOf("(");
      styleText = displayName.substring(idx);
      displayName = displayName.substring(0, idx).trim();
    }

    const chip = document.createElement("div");
    chip.className = `voice-chip ${isSaved ? 'saved-active' : ''} ${isSelected ? 'selected' : ''}`.trim();
    chip.dataset.id = voice.id;
    chip.title = `${voice.name} (${voice.provider})`;

    chip.innerHTML = `
      <div class="voice-chip-name">
        <span style="font-size:13px;">${genderIcon}</span>
        <span>${escapeHtml(displayName)}</span>
      </div>
      <div style="display:flex;align-items:center;gap:4px;">
        ${styleText ? `<span class="voice-chip-style">${escapeHtml(styleText)}</span>` : ''}
        ${isSaved ? '<span style="background:#23a55a;color:white;font-size:9px;padding:1px 4px;border-radius:3px;font-weight:700;">設定中</span>' : ''}
      </div>
    `;

    chip.addEventListener("click", () => {
      selectVoiceDetail(voice.id, false);
    });

    container.appendChild(chip);
  });

  // 初回または再描画時に右側詳細パネルを更新
  if (currentSelectedSpeakerId) {
    selectVoiceDetail(currentSelectedSpeakerId, false);
  }
}

function selectVoiceDetail(speakerId, scroll=false) {
  if (!speakerId) return;
  const isChanged = (currentSelectedSpeakerId !== speakerId);
  currentSelectedSpeakerId = speakerId;
  const savedSpeakerId = currentVoiceSettings ? currentVoiceSettings.speaker_id : "nanami";
  const isSaved = (speakerId === savedSpeakerId);

  // 別モデルを選択した時は、試聴キャッシュ（0% / 1.0x / 80%）が最速で効くように初期化
  // (現在保存中のモデルであれば、本人が設定・保存したピッチ・速度・音量を復元)
  if (isChanged) {
    if (isSaved && currentVoiceSettings) {
      currentDetailPitch = parseInt(currentVoiceSettings.pitch ? currentVoiceSettings.pitch.replace(/[%+]/g, "") : "0") || 0;
      currentDetailSpeed = parseFloat(currentVoiceSettings.speed) || 1.0;
      currentDetailVolume = (currentVoiceSettings.volume !== undefined && currentVoiceSettings.volume !== null) ? Math.round(parseFloat(currentVoiceSettings.volume) * 100) : 80;
    } else {
      currentDetailPitch = 0;
      currentDetailSpeed = 1.0;
      currentDetailVolume = 80;
    }
  } else if (isSaved && currentVoiceSettings) {
    if (currentVoiceSettings.volume !== undefined && currentVoiceSettings.volume !== null) {
      currentDetailVolume = Math.round(parseFloat(currentVoiceSettings.volume) * 100);
    }
  }

  // チップの選択状態を更新
  document.querySelectorAll(".voice-chip").forEach(c => {
    const isThis = (c.dataset.id === speakerId);
    c.classList.toggle("selected", isThis);
    if (isThis && scroll) {
      c.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  });

  const voice = availableVoices.find(v => v.id === speakerId);
  const detailContainer = document.getElementById("voice-detail-container");
  if (!voice || !detailContainer) return;

  // プロバイダーバッジ情報
  const providerColors = {
    voicevox: { bg: '#23a55a', label: 'VOICEVOX' },
    sharevox: { bg: '#f59e0b', label: 'SHAREVOX' },
    coeiroink: { bg: '#ec4899', label: 'COEIROINK' },
    openjtalk: { bg: '#0ea5e9', label: 'OpenJTalk' },
    google: { bg: '#ea4335', label: 'Google Cloud' },
    edge: { bg: '#00a4ef', label: 'Edge (無料)' },
    azure: { bg: '#0078d4', label: 'Azure Speech' }
  };
  const pInfo = providerColors[voice.provider] || { bg: '#5865f2', label: voice.provider };

  const genderLabel = voice.gender === 'female' ? '👧 女性ボイス' : (voice.gender === 'male' ? '👦 男性ボイス' : '🤖 中性/キャラクター');
  const genderClass = voice.gender === 'female' ? 'gender-female' : (voice.gender === 'male' ? 'gender-male' : 'gender-neutral');

  // 規約リンクの検索
  const termEntry = CHARACTER_TERMS_DATA.find(t => voice.name.includes(t.name.split(" ")[0]));
  const termUrl = termEntry ? termEntry.url : null;

  detailContainer.innerHTML = `
    <div style="margin-bottom:14px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px;">
      <div>
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:4px;">
          <span class="badge" style="background:${pInfo.bg};color:white;font-weight:700;font-size:11px;">${pInfo.label}</span>
          <span class="speaker-gender-badge ${genderClass}" style="font-size:11px;padding:2px 8px;">${genderLabel}</span>
          ${isSaved ? '<span class="badge" style="background:#23a55a;color:white;font-weight:700;font-size:11px;">現在設定中</span>' : ''}
        </div>
        <h3 style="font-size:18px;font-weight:700;margin:0;color:var(--text-main);">${escapeHtml(voice.name)}</h3>
      </div>
      <div style="width:48px;height:48px;border-radius:50%;background:rgba(255,255,255,0.06);display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0;">
        ${voice.gender === 'female' ? '👧' : (voice.gender === 'male' ? '👦' : '🎙️')}
      </div>
    </div>

    <div style="font-size:13px;color:var(--text-muted);line-height:1.5;margin-bottom:14px;background:rgba(0,0,0,0.2);padding:10px 12px;border-radius:6px;">
      ${escapeHtml(voice.description || '特徴的な表現力を持つ音声モデルです。')}
      ${termUrl ? `<div style="margin-top:6px;"><a href="${termUrl}" target="_blank" rel="noopener" style="color:var(--accent);font-size:11px;text-decoration:none;">利用規約・クレジット表記を確認 ↗</a></div>` : ''}
    </div>

    <!-- 試聴エリア -->
    <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:12px;margin-bottom:16px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
        <label class="form-label" style="font-size:12px;margin:0;">この声質でテスト試聴</label>
        <span style="font-size:10px;color:#23a55a;font-weight:600;">⚡ 0% / 1.0x は即時再生</span>
      </div>
      <div style="display:flex;gap:6px;">
        <input type="text" id="preview-text-input" class="form-input" style="font-size:12px;padding:6px 10px;" value="こんにちは！超低遅延で自然な音声をお届けします。" placeholder="試聴テキスト...">
        <button type="button" id="preview-btn" class="btn btn-secondary btn-sm" onclick="testPreviewAudio()" style="white-space:nowrap;padding:6px 12px;font-size:12px;">試聴 🔊</button>
      </div>
    </div>

    <!-- パラメーター調整 -->
    <div style="display:flex;flex-direction:column;gap:12px;margin-bottom:16px;">
      <!-- 音量 -->
      <div>
        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
          <span style="font-weight:600;">🔊 読み上げ音量</span>
          <span id="volume-val" class="badge" style="background:var(--primary);color:white;font-size:11px;">${currentDetailVolume}%</span>
        </div>
        <input type="range" id="volume-slider" class="slider" min="0" max="200" step="5" value="${currentDetailVolume}" oninput="updateVolumeVal(this.value)">
        <div style="display:flex;gap:4px;margin-top:4px;">
          <button type="button" class="btn btn-secondary btn-sm" onclick="setVolumePreset(50)" style="padding:1px 6px;font-size:10px;">50%</button>
          <button type="button" class="btn btn-primary btn-sm" onclick="setVolumePreset(80)" style="padding:1px 6px;font-size:10px;">80% (標準)</button>
          <button type="button" class="btn btn-secondary btn-sm" onclick="setVolumePreset(100)" style="padding:1px 6px;font-size:10px;">100%</button>
          <button type="button" class="btn btn-secondary btn-sm" onclick="setVolumePreset(150)" style="padding:1px 6px;font-size:10px;">150%</button>
        </div>
      </div>

      <!-- 速度 -->
      <div>
        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
          <span style="font-weight:600;">読み上げ速度</span>
          <span id="speed-val" class="form-val-badge" style="font-size:11px;">${Number(currentDetailSpeed).toFixed(2)}x</span>
        </div>
        <input type="range" id="speed-slider" class="slider" min="0.6" max="1.8" step="0.05" value="${currentDetailSpeed}" oninput="updateSpeedVal(this.value)">
      </div>

      <!-- ピッチ -->
      <div>
        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
          <span style="font-weight:600;">声の高さ (ピッチ)</span>
          <span id="pitch-val" class="form-val-badge" style="font-size:11px;">${currentDetailPitch > 0 ? '+' : ''}${currentDetailPitch}%</span>
        </div>
        <input type="range" id="pitch-slider" class="slider" min="-25" max="25" value="${currentDetailPitch}" oninput="updatePitchVal(this.value)">
      </div>

      <!-- リセットボタン -->
      <div style="display:flex;justify-content:flex-end;">
        <button type="button" class="btn btn-secondary btn-sm" onclick="resetPitchSpeedDefault()" style="font-size:11px;padding:3px 10px;color:var(--text-muted);">
          ↺ ピッチ・速度を初期値（最速キャッシュ）に戻す
        </button>
      </div>
    </div>

    <!-- 括弧除去チェック -->
    <div style="margin-bottom:18px;padding:8px 10px;background:rgba(255,255,255,0.02);border-radius:6px;">
      <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;">
        <input type="checkbox" id="strip-brackets-checkbox" ${currentDetailStrip ? 'checked' : ''} onchange="currentDetailStrip = this.checked;" style="accent-color:var(--primary);width:15px;height:15px;">
        <span>表示名の括弧【】や（）を除去して読み上げる</span>
      </label>
    </div>

    <!-- 保存ボタン -->
    <button type="button" id="save-voice-btn" class="btn btn-primary" onclick="saveVoiceSettings()" style="width:100%;padding:12px;font-size:14px;font-weight:700;">
      ★ この声質を保存する
    </button>
  `;
}

function updateVolumeVal(val) {
  currentDetailVolume = parseInt(val) || 80;
  const el = document.getElementById("volume-val");
  if (el) el.textContent = `${currentDetailVolume}%`;
}

function updateSpeedVal(val) {
  currentDetailSpeed = parseFloat(val) || 1.0;
  const el = document.getElementById("speed-val");
  if (el) el.textContent = `${Number(currentDetailSpeed).toFixed(2)}x`;
}

function updatePitchVal(val) {
  currentDetailPitch = parseInt(val) || 0;
  const el = document.getElementById("pitch-val");
  if (el) el.textContent = `${currentDetailPitch > 0 ? '+' : ''}${currentDetailPitch}%`;
}

function resetPitchSpeedDefault() {
  currentDetailPitch = 0;
  currentDetailSpeed = 1.0;
  currentDetailVolume = 80;
  const pitchSlider = document.getElementById("pitch-slider");
  if (pitchSlider) pitchSlider.value = "0";
  const speedSlider = document.getElementById("speed-slider");
  if (speedSlider) speedSlider.value = "1.0";
  const volumeSlider = document.getElementById("volume-slider");
  if (volumeSlider) volumeSlider.value = "80";
  updatePitchVal(0);
  updateSpeedVal(1.0);
  updateVolumeVal(80);
}

function selectVoiceCard(speakerId) {
  selectVoiceDetail(speakerId, true);
}

function initVoiceControls(settings) {
  if (!settings) return;

  // 設定値を内部状態変数に同期
  if (settings.pitch) {
    currentDetailPitch = parseInt(settings.pitch.replace(/[%+]/g, "")) || 0;
  }
  if (settings.speed) {
    currentDetailSpeed = parseFloat(settings.speed) || 1.0;
  }
  if (settings.volume !== undefined) {
    currentDetailVolume = Math.round(parseFloat(settings.volume) * 100);
  }
  if (settings.strip_name_brackets !== undefined) {
    currentDetailStrip = !!settings.strip_name_brackets;
  }
  if (settings.speaker_id) {
    currentSelectedSpeakerId = settings.speaker_id;
  }

  // DOM要素が存在する場合のみ安全に値をセット
  const pitchSlider = document.getElementById("pitch-slider");
  const pitchVal = document.getElementById("pitch-val");
  if (pitchSlider) {
    pitchSlider.value = currentDetailPitch;
  }
  if (pitchVal) {
    pitchVal.textContent = (currentDetailPitch >= 0 ? `+${currentDetailPitch}` : currentDetailPitch) + "%";
  }

  const speedSlider = document.getElementById("speed-slider");
  const speedVal = document.getElementById("speed-val");
  if (speedSlider) {
    speedSlider.value = currentDetailSpeed;
  }
  if (speedVal) {
    speedVal.textContent = Number(currentDetailSpeed).toFixed(2) + "x";
  }

  const volumeSlider = document.getElementById("volume-slider");
  const volumeVal = document.getElementById("volume-val");
  if (volumeSlider) {
    volumeSlider.value = currentDetailVolume;
  }
  if (volumeVal) {
    volumeVal.textContent = currentDetailVolume + "%";
  }

  const stripCheckbox = document.getElementById("strip-brackets-checkbox");
  if (stripCheckbox) {
    stripCheckbox.checked = currentDetailStrip;
  }

  // 話者一覧が既にロードされていれば、詳細パネルを描画
  if (availableVoices && availableVoices.length > 0 && currentSelectedSpeakerId) {
    selectVoiceDetail(currentSelectedSpeakerId, false);
  }
}

// 音量プリセット設定
function setVolumePreset(val) {
  const slider = document.getElementById("volume-slider");
  const valEl = document.getElementById("volume-val");
  if (slider) slider.value = val;
  if (valEl) valEl.textContent = val + "%";
}
window.setVolumePreset = setVolumePreset;

// 個人設定の保存
async function saveVoiceSettings() {
  const speakerId = currentSelectedSpeakerId || (document.querySelector(".voice-chip.selected")?.dataset.id);
  if (!speakerId) {
    showToast("話者を選択してください。", "error");
    return;
  }

  const pitchSlider = document.getElementById("pitch-slider");
  const speedSlider = document.getElementById("speed-slider");
  const volumeSlider = document.getElementById("volume-slider");
  const stripCheckbox = document.getElementById("strip-brackets-checkbox");

  const pitch = pitchSlider ? ((parseInt(pitchSlider.value) >= 0 ? `+${pitchSlider.value}` : pitchSlider.value) + "%") : "+0%";
  const speed = speedSlider ? parseFloat(speedSlider.value) : 1.0;
  const volume = volumeSlider ? parseFloat(volumeSlider.value) / 100.0 : 0.8;
  const stripNameBrackets = stripCheckbox ? stripCheckbox.checked : true;

  const saveBtn = document.getElementById("save-voice-btn");
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = "保存中...";
  }

  try {
    const res = await fetch("/api/me/voice", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        speaker_id: speakerId,
        pitch: pitch,
        speed: speed,
        volume: volume,
        strip_name_brackets: stripNameBrackets,
        guild_id: currentGuildId
      })
    });
    const data = await res.json();
    if (res.ok) {
      showToast("音声設定を正常に保存しました！", "success");
      currentVoiceSettings = data.voice;
      currentSelectedSpeakerId = currentVoiceSettings.speaker_id;
      if (currentVoiceSettings.volume !== undefined && currentVoiceSettings.volume !== null) {
        currentDetailVolume = Math.round(parseFloat(currentVoiceSettings.volume) * 100);
      }
      loadSyncSourceGuilds();
      renderVoiceChips();
      selectVoiceDetail(currentSelectedSpeakerId, false);
    } else {
      showToast(data.error || "保存に失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = "★ この声質を保存する";
    }
  }
}

// サーバー間同期用: 同期元サーバー一覧の読み込み
async function loadSyncSourceGuilds() {
  try {
    const res = await fetch("/api/me/voices_by_guild");
    if (!res.ok) return;
    const data = await res.json();
    const selectEl = document.getElementById("sync-source-guild-select");
    if (!selectEl) return;

    selectEl.innerHTML = '<option value="">同期元のサーバーを選択...</option>';
    const guilds = data.guilds || [];
    guilds.forEach(g => {
      // 現在のサーバー以外を選択肢として表示
      if (g.guild_id !== currentGuildId) {
        const opt = document.createElement("option");
        opt.value = g.guild_id;
        const speakerLabel = g.speaker_name ? ` (${g.speaker_name})` : '';
        opt.textContent = `${g.guild_name}${speakerLabel}`;
        selectEl.appendChild(opt);
      }
    });
  } catch (e) {
    console.error("Failed to load sync source guilds:", e);
  }
}

// サーバー間同期の実行
async function syncVoiceFromGuild() {
  const selectEl = document.getElementById("sync-source-guild-select");
  const fromGuildId = selectEl ? selectEl.value : "";
  if (!fromGuildId) {
    showToast("同期元のサーバーを選択してください。", "error");
    return;
  }
  if (!currentGuildId) {
    showToast("同期先のサーバーが選択されていません。", "error");
    return;
  }

  const fromOpt = selectEl.options[selectEl.selectedIndex];
  const fromName = fromOpt ? fromOpt.textContent : "選択したサーバー";
  const currentG = userGuilds.find(g => g.id === currentGuildId);
  const currentName = currentG ? currentG.name : "現在のサーバー";

  if (!confirm(`「${fromName}」の音声設定を、「${currentName}」に同期・反映しますか？`)) {
    return;
  }

  try {
    const res = await fetch("/api/me/sync_voice", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        from_guild_id: fromGuildId,
        to_guild_id: currentGuildId
      })
    });
    const data = await res.json();
    if (res.ok) {
      showToast("音声設定を同期しました！", "success");
      currentVoiceSettings = data.voice;
      initVoiceControls(currentVoiceSettings);
      if (data.voice.speaker_id) {
        selectVoiceCard(data.voice.speaker_id);
      }
      loadSyncSourceGuilds();
    } else {
      showToast(data.error || "設定の同期に失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  }
}

// ブラウザでの試聴再生
async function testPreviewAudio() {
  const speakerId = currentSelectedSpeakerId || (document.querySelector(".voice-chip.selected")?.dataset.id) || (currentVoiceSettings?.speaker_id || "nanami");
  const pitchSlider = document.getElementById("pitch-slider");
  const speedSlider = document.getElementById("speed-slider");
  const previewInput = document.getElementById("preview-text-input");

  const pitch = (parseInt(pitchSlider.value) >= 0 ? `+${pitchSlider.value}` : pitchSlider.value) + "%";
  const speed = parseFloat(speedSlider.value);
  const text = previewInput.value.trim() || "こんにちは！超低遅延で自然な音声をお届けします。";

  const btn = document.getElementById("preview-btn");
  btn.disabled = true;
  btn.textContent = "生成中...";

  try {
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: text,
        speaker_id: speakerId,
        pitch: pitch,
        speed: speed
      })
    });

    if (res.ok) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      previewAudio.src = url;
      const volumeSlider = document.getElementById("volume-slider");
      if (volumeSlider) {
        previewAudio.volume = Math.max(0.0, Math.min(parseFloat(volumeSlider.value) / 100.0, 1.0));
      }
      previewAudio.play();
      btn.textContent = "再生中 🔊";
      previewAudio.onended = () => {
        btn.disabled = false;
        btn.textContent = "試聴する 🔊";
      };
    } else {
      const err = await res.json();
      showToast(err.error || "音声の生成に失敗しました", "error");
      btn.disabled = false;
      btn.textContent = "試聴する 🔊";
    }
  } catch (e) {
    showToast("音声試聴中に通信エラーが発生しました", "error");
    btn.disabled = false;
    btn.textContent = "試聴する 🔊";
  }
}

// --- NGワード管理 ---

let allNgWords = [];
let currentFilteredNgWords = [];

async function loadNgWords(guildId) {
  if (!guildId) return;
  try {
    const res = await fetch(`/api/ng?guild_id=${guildId}`);
    const data = await res.json();
    allNgWords = data.ng_words || [];
    filterNgWords();
  } catch (e) {
    console.error("Failed to load NG words:", e);
  }
}

function filterNgWords() {
  const query = (document.getElementById("ng-search-input")?.value || "").trim().toLowerCase();
  if (!query) {
    currentFilteredNgWords = [...allNgWords];
  } else {
    currentFilteredNgWords = allNgWords.filter(w => w.toLowerCase().includes(query));
  }
  renderNgTable(currentFilteredNgWords);
}

function renderNgTable(words) {
  const tbody = document.getElementById("ng-table-body");
  const countSpan = document.getElementById("ng-count");
  if (!tbody) return;

  if (countSpan) countSpan.textContent = `${words.length}件`;
  tbody.innerHTML = "";

  if (words.length === 0) {
    tbody.innerHTML = `<tr><td colspan="2" style="text-align:center;color:var(--text-muted);padding:24px;">登録されているNGワードはありません</td></tr>`;
    return;
  }

  words.forEach(word => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="font-weight:600;color:#ff4757;">🚫 ${escapeHtml(word)}</td>
      <td style="text-align:right;white-space:nowrap;">
        <button type="button" class="btn btn-danger btn-sm" onclick="deleteNgWord('${encodeURIComponent(word)}')">削除</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

async function addNgWord(e) {
  if (e) e.preventDefault();
  const input = document.getElementById("ng-new-word");
  const word = input?.value.trim();
  if (!word || !currentGuildId) return;

  try {
    const res = await fetch("/api/ng", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guild_id: currentGuildId, word })
    });
    if (res.ok) {
      showToast(`NGワード「${word}」を追加しました 🚫`, "success");
      input.value = "";
      loadNgWords(currentGuildId);
    } else {
      const err = await res.json();
      showToast(err.error || "NGワードの追加に失敗しました", "error");
    }
  } catch (err) {
    console.error("Failed to add NG word:", err);
    showToast("通信エラーが発生しました", "error");
  }
}

async function deleteNgWord(encodedWord) {
  const word = decodeURIComponent(encodedWord);
  if (!confirm(`NGワード「${word}」を削除しますか？`)) return;
  if (!currentGuildId) return;

  try {
    const res = await fetch(`/api/ng/${encodeURIComponent(word)}?guild_id=${currentGuildId}`, {
      method: "DELETE"
    });
    if (res.ok) {
      showToast(`NGワード「${word}」を削除しました`, "success");
      loadNgWords(currentGuildId);
    } else {
      showToast("NGワードの削除に失敗しました", "error");
    }
  } catch (err) {
    console.error("Failed to delete NG word:", err);
    showToast("通信エラーが発生しました", "error");
  }
}

async function importDefaultNgWords() {
  if (!currentGuildId) return;
  if (!confirm("一般的な標準NGワードプリセット（差別用語・露骨な性的語句等）を一括登録しますか？\n（既に登録済みの単語は重複登録されません）")) {
    return;
  }

  try {
    const res = await fetch("/api/ng/import_preset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guild_id: currentGuildId })
    });
    if (res.ok) {
      const data = await res.json();
      showToast(`標準NGワード ${data.imported_count} 件を一括登録しました 📋`, "success");
      loadNgWords(currentGuildId);
    } else {
      showToast("プリセットの登録に失敗しました", "error");
    }
  } catch (err) {
    console.error("Failed to import default NG words:", err);
    showToast("通信エラーが発生しました", "error");
  }
}

// --- 単語辞書管理 ---

let allDictWords = [];
let currentFilteredWords = [];
let currentlyEditingWord = null;

async function loadDictionary(guildId) {
  if (!guildId) return;
  try {
    const res = await fetch(`/api/dict?guild_id=${guildId}`);
    const data = await res.json();
    allDictWords = data.dictionary || [];
    currentlyEditingWord = null;
    applyDictFilter();
    // AI提案単語も並行してロード
    loadAiSuggestions(guildId);
  } catch (e) {
    console.error("Failed to load dictionary:", e);
  }
}

let currentAiSuggestions = [];

async function loadAiSuggestions(guildId) {
  if (!guildId) return;
  try {
    const res = await fetch(`/api/dict/ai/suggestions?guild_id=${guildId}`);
    if (!res.ok) return;
    const data = await res.json();
    currentAiSuggestions = data.suggestions || [];
    renderAiSuggestions(currentAiSuggestions);
  } catch (e) {
    console.error("Failed to load AI suggestions:", e);
  }
}

function renderAiSuggestions(suggestions) {
  const container = document.getElementById("ai-sugg-list");
  const countBadge = document.getElementById("ai-sugg-count");
  if (!container) return;

  if (countBadge) {
    countBadge.textContent = `${suggestions.length}件`;
    countBadge.style.background = suggestions.length > 0 ? "#5865f2" : "#4e5058";
  }

  if (suggestions.length === 0) {
    container.innerHTML = `
      <div style="font-size:13px;color:var(--text-muted);padding:10px 0;text-align:center;">
        現在、未承認のAI学習候補はありません。ユーザーの未知語発言や ❌ リアクションから自動提案されます。
      </div>
    `;
    return;
  }

  container.innerHTML = "";
  suggestions.forEach(item => {
    const card = document.createElement("div");
    card.style.cssText = "display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:10px 14px;gap:12px;flex-wrap:wrap;";

    const sourceBadge = item.source === "reaction"
      ? `<span class="badge" style="background:#e67e22;color:white;font-size:10px;">❌ リアクション指摘</span>`
      : `<span class="badge" style="background:#3498db;color:white;font-size:10px;">💡 未知語検知</span>`;

    const reasonHtml = item.reason
      ? `<div style="font-size:11px;color:var(--text-muted);margin-top:2px;">理由: ${escapeHtml(item.reason)}</div>`
      : "";

    card.innerHTML = `
      <div style="flex:1;min-width:200px;">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span style="font-weight:700;font-size:14px;color:var(--text-normal);">${escapeHtml(item.word)}</span>
          <span style="color:var(--text-muted);">➔</span>
          <span style="font-weight:700;font-size:14px;color:#23a55a;">${escapeHtml(item.suggested_reading)}</span>
          ${sourceBadge}
        </div>
        ${reasonHtml}
      </div>
      <div style="display:flex;gap:8px;">
        <button type="button" class="btn btn-primary btn-sm" style="background:#23a55a;border-color:#23a55a;" onclick="approveAiSuggestion(${item.id})">
          ✅ 承認して登録
        </button>
        <button type="button" class="btn btn-secondary btn-sm" style="color:#ed4245;" onclick="rejectAiSuggestion(${item.id})">
          ✕ 破棄
        </button>
      </div>
    `;
    container.appendChild(card);
  });
}

async function approveAiSuggestion(id) {
  try {
    const res = await fetch(`/api/dict/ai/suggestions/${id}/approve`, { method: "POST" });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`単語「${data.approved.word}」➔「${data.approved.suggested_reading}」を辞書に登録しました`, "success");
      const curGuild = currentGuildId;
      await loadDictionary(curGuild);
      await loadAiSuggestions(curGuild);
    } else {
      showToast(data.error || "承認に失敗しました", "error");
    }
  } catch (e) {
    console.error("Failed to approve AI suggestion:", e);
    showToast("承認中にエラーが発生しました", "error");
  }
}

async function rejectAiSuggestion(id) {
  try {
    const res = await fetch(`/api/dict/ai/suggestions/${id}/reject`, { method: "POST" });
    if (res.ok) {
      showToast("提案を破棄しました", "info");
      const curGuild = currentGuildId;
      await loadAiSuggestions(curGuild);
    } else {
      showToast("破棄に失敗しました", "error");
    }
  } catch (e) {
    console.error("Failed to reject AI suggestion:", e);
    showToast("破棄中にエラーが発生しました", "error");
  }
}

function applyDictFilter() {
  const searchInput = document.getElementById("dict-search-input");
  const query = searchInput ? searchInput.value.toLowerCase().trim() : "";
  if (query) {
    currentFilteredWords = allDictWords.filter(item => 
      item.word.toLowerCase().includes(query) || item.reading.toLowerCase().includes(query)
    );
  } else {
    currentFilteredWords = [...allDictWords];
  }
  renderDictionaryTable(currentFilteredWords);
}

function filterDictionary() {
  currentlyEditingWord = null;
  applyDictFilter();
}

let dictPreviewAudio = null;

async function playDictWordPreview(encodedReading, btn) {
  let reading = "";
  try {
    reading = decodeURIComponent(encodedReading);
  } catch (e) {
    reading = encodedReading;
  }
  if (!reading) return;

  if (dictPreviewAudio) {
    dictPreviewAudio.pause();
    dictPreviewAudio = null;
  }

  const origText = btn ? btn.textContent : "🔊 試聴";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⌛ 生成中";
  }

  try {
    const speakerId = currentVoiceSettings ? currentVoiceSettings.speaker_id : "nanami";
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: reading,
        speaker_id: speakerId,
        pitch: "0%",
        speed: 1.0
      })
    });

    if (res.ok) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      dictPreviewAudio = new Audio(url);
      dictPreviewAudio.volume = 0.8;
      dictPreviewAudio.play();
      if (btn) btn.textContent = "🔊 再生中";
      dictPreviewAudio.onended = () => {
        if (btn) {
          btn.disabled = false;
          btn.textContent = origText;
        }
      };
      dictPreviewAudio.onerror = () => {
        if (btn) {
          btn.disabled = false;
          btn.textContent = origText;
        }
      };
    } else {
      showToast("試聴音声の生成に失敗しました", "error");
      if (btn) {
        btn.disabled = false;
        btn.textContent = origText;
      }
    }
  } catch (e) {
    console.error("Dict preview error:", e);
    showToast("試聴リクエスト中にエラーが発生しました", "error");
    if (btn) {
      btn.disabled = false;
      btn.textContent = origText;
    }
  }
}

function renderDictionaryTable(words) {
  const tbody = document.getElementById("dict-table-body");
  const countSpan = document.getElementById("dict-count");
  if (!tbody) return;

  if (countSpan) countSpan.textContent = `${words.length}件`;
  tbody.innerHTML = "";

  if (words.length === 0) {
    tbody.innerHTML = `<tr><td colspan="3" style="text-align:center;color:var(--text-muted);padding:24px;">登録されている単語はありません</td></tr>`;
    return;
  }

  words.forEach((item, idx) => {
    const tr = document.createElement("tr");
    tr.id = `dict-row-${idx}`;

    if (currentlyEditingWord !== null && currentlyEditingWord === item.word) {
      // 編集モード
      tr.innerHTML = `
        <td style="padding:6px 12px;">
          <input type="text" id="edit-word-input" class="form-input" style="padding:6px 10px;font-size:13px;width:100%;font-weight:600;" value="${escapeHtml(item.word)}" placeholder="単語" required>
        </td>
        <td style="padding:6px 12px;">
          <input type="text" id="edit-reading-input" class="form-input" style="padding:6px 10px;font-size:13px;width:100%;" value="${escapeHtml(item.reading)}" placeholder="読み方" required>
        </td>
        <td style="text-align:right;white-space:nowrap;padding:6px 12px;">
          <button type="button" class="btn btn-secondary btn-sm" onclick="playDictWordPreview(encodeURIComponent(document.getElementById('edit-reading-input').value), this)" title="入力中の読み方を試聴" style="margin-right:6px;">🔊 試聴</button>
          <button type="button" class="btn btn-primary btn-sm" onclick="saveEditDictWord(${idx})" style="margin-right:6px;">保存</button>
          <button type="button" class="btn btn-secondary btn-sm" onclick="cancelEditDictWord()">取消</button>
        </td>
      `;
    } else {
      // 通常表示
      tr.innerHTML = `
        <td style="font-weight:600;">${escapeHtml(item.word)}</td>
        <td>${escapeHtml(item.reading)}</td>
        <td style="text-align:right;white-space:nowrap;">
          <button type="button" class="btn btn-secondary btn-sm" onclick="playDictWordPreview('${encodeURIComponent(item.reading)}', this)" title="読み方を試聴" style="margin-right:6px;">🔊 試聴</button>
          <button type="button" class="btn btn-secondary btn-sm" onclick="startEditDictWord(${idx})" style="margin-right:6px;">編集</button>
          <button type="button" class="btn btn-danger btn-sm" onclick="deleteDictWord('${encodeURIComponent(item.word)}')">削除</button>
        </td>
      `;
    }
    tbody.appendChild(tr);
  });

  // 編集モードの場合、入力欄へのフォーカスとキーバインド設定
  if (currentlyEditingWord !== null) {
    const wordInp = document.getElementById("edit-word-input");
    const readInp = document.getElementById("edit-reading-input");
    const onKey = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const curIdx = currentFilteredWords.findIndex(w => w.word === currentlyEditingWord);
        if (curIdx !== -1) saveEditDictWord(curIdx);
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancelEditDictWord();
      }
    };
    if (wordInp) {
      wordInp.addEventListener("keydown", onKey);
      wordInp.focus();
      wordInp.select();
    }
    if (readInp) {
      readInp.addEventListener("keydown", onKey);
    }
  }
}

function startEditDictWord(idx) {
  const item = currentFilteredWords[idx];
  if (!item) return;
  currentlyEditingWord = item.word;
  renderDictionaryTable(currentFilteredWords);
}

function cancelEditDictWord() {
  currentlyEditingWord = null;
  renderDictionaryTable(currentFilteredWords);
}

async function saveEditDictWord(idx) {
  if (!currentGuildId) {
    showToast("サーバーが選択されていません", "error");
    return;
  }
  const item = currentFilteredWords[idx];
  if (!item) return;

  const wordInp = document.getElementById("edit-word-input");
  const readInp = document.getElementById("edit-reading-input");
  if (!wordInp || !readInp) return;

  const newWord = wordInp.value.trim();
  const newReading = readInp.value.trim();

  if (!newWord || !newReading) {
    showToast("単語と読みの両方を入力してください", "error");
    return;
  }

  // 変更がない場合は単に閉じる
  if (newWord === item.word && newReading === item.reading) {
    cancelEditDictWord();
    return;
  }

  try {
    const res = await fetch("/api/dict", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: currentGuildId,
        old_word: item.word,
        new_word: newWord,
        reading: newReading
      })
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`単語「${newWord}」を更新しました（キャッシュ${data.purged_cache_count}件パージ）`, "success");
      currentlyEditingWord = null;
      await loadDictionary(currentGuildId);
    } else {
      showToast(data.error || "更新に失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  }
}

async function addDictWord(e) {
  e.preventDefault();
  if (!currentGuildId) {
    showToast("サーバーが選択されていません", "error");
    return;
  }

  const wordInput = document.getElementById("dict-new-word");
  const readingInput = document.getElementById("dict-new-reading");

  const word = wordInput.value.trim();
  const reading = readingInput.value.trim();

  if (!word || !reading) {
    showToast("単語と読みの両方を入力してください", "error");
    return;
  }

  try {
    const res = await fetch("/api/dict", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: currentGuildId,
        word: word,
        reading: reading
      })
    });
    const data = await res.json();

    if (res.ok) {
      showToast(`単語「${word}」を登録しました（キャッシュ${data.purged_cache_count}件パージ）`, "success");
      wordInput.value = "";
      readingInput.value = "";
      loadDictionary(currentGuildId);
    } else {
      showToast(data.error || "登録に失敗しました", "error");
    }
  } catch (err) {
    showToast("通信エラーが発生しました", "error");
  }
}

async function deleteDictWord(encodedWord) {
  const word = decodeURIComponent(encodedWord);
  if (!confirm(`単語「${word}」を辞書から削除しますか？`)) return;

  try {
    const res = await fetch(`/api/dict/${encodedWord}?guild_id=${currentGuildId}`, { method: "DELETE" });
    const data = await res.json();
    if (res.ok) {
      showToast(`単語「${word}」を削除しました`, "success");
      loadDictionary(currentGuildId);
    } else {
      showToast(data.error || "削除に失敗しました", "error");
    }
  } catch (err) {
    showToast("削除中にエラーが発生しました", "error");
  }
}

// --- 単語辞書テキスト一括インポート ---

function parseBatchDictText(rawText) {
  if (!rawText) return { validItems: [], skippedLines: [] };
  const lines = rawText.split(/\r?\n/);
  const itemsMap = new Map();
  const skippedLines = [];

  for (let i = 0; i < lines.length; i++) {
    const rawLine = lines[i].trim();
    if (!rawLine) continue;

    let sepIdx = -1;
    let sepLen = 1;

    const zenColIdx = rawLine.lastIndexOf('：');
    const hanColIdx = rawLine.lastIndexOf(':');
    const tabIdx = rawLine.lastIndexOf('\t');

    if (zenColIdx !== -1 && (hanColIdx === -1 || zenColIdx >= hanColIdx)) {
      sepIdx = zenColIdx;
    } else if (hanColIdx !== -1) {
      sepIdx = hanColIdx;
    } else if (tabIdx !== -1) {
      sepIdx = tabIdx;
    }

    if (sepIdx > 0 && sepIdx < rawLine.length - 1) {
      const word = rawLine.slice(0, sepIdx).trim();
      const reading = rawLine.slice(sepIdx + sepLen).trim();
      if (word && reading) {
        itemsMap.set(word, { word, reading, lineNum: i + 1 });
        continue;
      }
    }

    skippedLines.push({ line: rawLine, lineNum: i + 1 });
  }

  return { validItems: Array.from(itemsMap.values()), skippedLines };
}

let dictConflictSelections = new Map();

function openDictBatchImportModal() {
  const modal = document.getElementById("dict-batch-modal");
  if (modal) {
    modal.style.display = "flex";
    dictConflictSelections.clear();
    const textarea = document.getElementById("dict-batch-textarea");
    if (textarea) {
      textarea.focus();
      onDictBatchInputChanged();
    }
  }
}

function closeDictBatchImportModal(e) {
  if (e && e.target !== e.currentTarget && !e.target.classList.contains("btn")) return;
  const modal = document.getElementById("dict-batch-modal");
  if (modal) modal.style.display = "none";
}

function onDictConflictChange(word, overwrite) {
  dictConflictSelections.set(word, overwrite);
  onDictBatchInputChanged(false); // 入力再パースはせずUI再描画
}

function setAllDictConflicts(overwrite) {
  const textarea = document.getElementById("dict-batch-textarea");
  if (!textarea) return;
  const { validItems } = parseBatchDictText(textarea.value);
  const existingMap = new Map((allDictWords || []).map(d => [d.word, d.reading]));
  validItems.forEach(item => {
    const existing = existingMap.get(item.word);
    if (existing !== undefined && existing !== item.reading) {
      dictConflictSelections.set(item.word, overwrite);
    }
  });
  onDictBatchInputChanged(false);
}

function onDictBatchInputChanged(clearSelections = true) {
  const textarea = document.getElementById("dict-batch-textarea");
  const container = document.getElementById("dict-batch-preview-container");
  const badgeNew = document.getElementById("dict-batch-badge-new");
  const badgeConflict = document.getElementById("dict-batch-badge-conflict");
  const badgeIdentical = document.getElementById("dict-batch-badge-identical");
  const conflictActions = document.getElementById("dict-batch-conflict-actions");
  const skipInfoEl = document.getElementById("dict-batch-skip-info");
  const tbody = document.getElementById("dict-batch-preview-tbody");
  const submitBtn = document.getElementById("dict-batch-submit-btn");

  if (!textarea) return;
  const { validItems, skippedLines } = parseBatchDictText(textarea.value);

  if (clearSelections) {
    // 不要になった単語をクリーンアップ
    const currentWords = new Set(validItems.map(it => it.word));
    for (const k of dictConflictSelections.keys()) {
      if (!currentWords.has(k)) dictConflictSelections.delete(k);
    }
  }

  if (validItems.length === 0 && skippedLines.length === 0) {
    if (container) container.style.display = "none";
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "インポートを実行 (0件)";
    }
    return;
  }

  if (container) container.style.display = "block";

  // 既存辞書との突合判定
  const existingMap = new Map((allDictWords || []).map(d => [d.word, d.reading]));
  let newCount = 0;
  let conflictCount = 0;
  let identicalCount = 0;
  let toImportCount = 0;

  const analyzedItems = validItems.map(item => {
    const existing = existingMap.get(item.word);
    let status = "new"; // "new" | "conflict" | "identical"
    if (existing === undefined) {
      status = "new";
      newCount++;
      toImportCount++;
    } else if (existing === item.reading) {
      status = "identical";
      identicalCount++;
    } else {
      status = "conflict";
      conflictCount++;
      // デフォルトは上書き(true)
      const overwrite = dictConflictSelections.has(item.word) ? dictConflictSelections.get(item.word) : true;
      if (overwrite) {
        toImportCount++;
      }
    }
    return { ...item, existingReading: existing, status };
  });

  if (badgeNew) {
    badgeNew.textContent = `新規: ${newCount}件`;
  }
  if (badgeConflict) {
    badgeConflict.textContent = `競合: ${conflictCount}件`;
    badgeConflict.style.display = conflictCount > 0 ? "inline-block" : "none";
  }
  if (badgeIdentical) {
    badgeIdentical.textContent = `完全重複(スキップ): ${identicalCount}件`;
  }
  if (conflictActions) {
    conflictActions.style.display = conflictCount > 0 ? "flex" : "none";
  }

  if (skipInfoEl) {
    if (skippedLines.length > 0) {
      skipInfoEl.textContent = `※書式スキップ: ${skippedLines.length}行`;
      skipInfoEl.style.display = "inline";
    } else {
      skipInfoEl.style.display = "none";
    }
  }

  if (tbody) {
    tbody.innerHTML = "";
    const displayItems = analyzedItems.slice(0, 100);
    displayItems.forEach(item => {
      const tr = document.createElement("tr");
      tr.style.borderBottom = "1px solid rgba(255,255,255,0.05)";

      let statusHtml = "";
      let existingHtml = "";

      if (item.status === "new") {
        statusHtml = `<span class="badge" style="background:#23a55a;color:white;font-size:10px;">新規追加</span>`;
        existingHtml = `<span style="color:var(--text-dim);font-size:11px;">未登録</span>`;
      } else if (item.status === "identical") {
        tr.style.opacity = "0.5";
        statusHtml = `<span class="badge" style="background:#5c6069;color:white;font-size:10px;">完全重複 (除外)</span>`;
        existingHtml = `<span style="color:var(--text-muted);font-size:11px;">登録済み (${escapeHtml(item.existingReading)})</span>`;
      } else {
        // 競合
        const overwrite = dictConflictSelections.has(item.word) ? dictConflictSelections.get(item.word) : true;
        statusHtml = `
          <select class="input-field" style="font-size:11px;padding:2px 4px;width:auto;background:${overwrite ? 'rgba(235, 100, 52, 0.2)' : 'rgba(255, 255, 255, 0.08)'};border-color:${overwrite ? '#eb6434' : 'var(--border-color)'};"
            data-word="${escapeHtml(item.word)}"
            onchange="onDictConflictChange(this.getAttribute('data-word'), this.value === 'overwrite')">
            <option value="overwrite" ${overwrite ? 'selected' : ''}>⚠️ 上書き</option>
            <option value="skip" ${!overwrite ? 'selected' : ''}>⏭️ スキップ</option>
          </select>
        `;
        existingHtml = `<span style="color:#eb6434;font-size:11px;font-weight:600;">既存: ${escapeHtml(item.existingReading)}</span>`;
      }

      tr.innerHTML = `
        <td style="padding:6px 8px;">${statusHtml}</td>
        <td style="padding:6px 8px;word-break:break-all;color:var(--text-main);font-weight:500;">${escapeHtml(item.word)}</td>
        <td style="padding:6px 8px;word-break:break-all;color:var(--primary);font-weight:600;">${escapeHtml(item.reading)}</td>
        <td style="padding:6px 8px;word-break:break-all;">${existingHtml}</td>
      `;
      tbody.appendChild(tr);
    });

    if (analyzedItems.length > 100) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="4" style="padding:8px;text-align:center;color:var(--text-muted);font-size:11px;">...他 ${analyzedItems.length - 100} 件省略</td>`;
      tbody.appendChild(tr);
    }
  }

  if (submitBtn) {
    submitBtn.disabled = toImportCount === 0;
    submitBtn.textContent = `インポートを実行 (${toImportCount}件)`;
  }
}

async function executeDictBatchImport() {
  if (!currentGuildId) {
    showToast("サーバーが選択されていません", "error");
    return;
  }

  const textarea = document.getElementById("dict-batch-textarea");
  if (!textarea) return;

  const { validItems } = parseBatchDictText(textarea.value);
  const existingMap = new Map((allDictWords || []).map(d => [d.word, d.reading]));

  // 完全重複は除外、競合はoverwriteがtrueのもののみ、新規は含める
  const itemsToSubmit = [];
  validItems.forEach(item => {
    const existing = existingMap.get(item.word);
    if (existing === undefined) {
      itemsToSubmit.push({ word: item.word, reading: item.reading });
    } else if (existing === item.reading) {
      // 完全重複はスキップ（スルー）
    } else {
      const overwrite = dictConflictSelections.has(item.word) ? dictConflictSelections.get(item.word) : true;
      if (overwrite) {
        itemsToSubmit.push({ word: item.word, reading: item.reading });
      }
    }
  });

  if (itemsToSubmit.length === 0) {
    showToast("インポート対象がありません（すべて登録済みまたはスキップされました）", "warning");
    return;
  }

  const submitBtn = document.getElementById("dict-batch-submit-btn");
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = "インポート中...";
  }

  try {
    const payload = {
      guild_id: currentGuildId,
      items: itemsToSubmit
    };

    const res = await fetch("/api/dict/import_batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (res.ok) {
      showToast(`${data.imported_count}件の単語をインポートしました！`, "success");
      textarea.value = "";
      dictConflictSelections.clear();
      onDictBatchInputChanged();
      closeDictBatchImportModal();
      loadDictionary(currentGuildId);
    } else {
      showToast(data.error || "インポートに失敗しました", "error");
    }
  } catch (e) {
    console.error("Batch import error:", e);
    showToast("通信エラーが発生しました", "error");
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      onDictBatchInputChanged(false);
    }
  }
}

// --- ユーザー名インポートモーダル ---

function openUserImportModal() {
  const preview = document.getElementById("import-my-name-preview");
  if (preview) {
    preview.textContent = currentUserName || "ログイン中ユーザー";
  }
  const modal = document.getElementById("user-import-modal");
  if (modal) modal.style.display = "flex";
}

function closeUserImportModal(e) {
  if (e && e.target !== e.currentTarget && !e.target.classList.contains("btn")) return;
  const modal = document.getElementById("user-import-modal");
  if (modal) modal.style.display = "none";
}

async function executeUserImport(scope) {
  if (!currentGuildId) {
    showToast("サーバーが選択されていません", "error");
    return;
  }

  try {
    const res = await fetch("/api/dict/import_users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: currentGuildId,
        scope: scope
      })
    });

    const data = await res.json();
    if (res.ok) {
      const scopeLabel = scope === "me" ? "あなたの表示名" : "サーバー全員の表示名";
      showToast(`${scopeLabel} をインポートしました（新規追加: ${data.imported_count}件）`, "success");
      closeUserImportModal();
      loadDictionary(currentGuildId);
    } else {
      showToast(data.error || "インポートに失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  }
}

// --- 単語辞書テキスト一括インポートモーダル ---
let parsedBatchDictItems = [];

function openDictBatchImportModal() {
  const modal = document.getElementById("dict-batch-modal");
  if (!modal) return;
  const textarea = document.getElementById("dict-batch-textarea");
  if (textarea) textarea.value = "";
  onDictBatchInputChanged();
  modal.style.display = "flex";
  if (textarea) textarea.focus();
}

function closeDictBatchImportModal(e) {
  if (e && e.target !== e.currentTarget && !e.target.classList.contains("btn")) return;
  const modal = document.getElementById("dict-batch-modal");
  if (modal) modal.style.display = "none";
}

function onDictBatchInputChanged() {
  const textarea = document.getElementById("dict-batch-textarea");
  const previewContainer = document.getElementById("dict-batch-preview-container");
  const countSpan = document.getElementById("dict-batch-valid-count");
  const skipSpan = document.getElementById("dict-batch-skip-info");
  const tbody = document.getElementById("dict-batch-preview-tbody");
  const submitBtn = document.getElementById("dict-batch-submit-btn");

  if (!textarea || !previewContainer || !countSpan || !tbody || !submitBtn) return;

  const rawText = textarea.value;
  const lines = rawText.split(/\r?\n/);
  parsedBatchDictItems = [];
  let skippedCount = 0;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;

    // 全角コロン「：」または半角コロン「:」の最初に出てくる位置を検出
    const halfIdx = line.indexOf(":");
    const fullIdx = line.indexOf("：");
    let colonIdx = -1;
    if (halfIdx !== -1 && fullIdx !== -1) {
      colonIdx = Math.min(halfIdx, fullIdx);
    } else if (halfIdx !== -1) {
      colonIdx = halfIdx;
    } else if (fullIdx !== -1) {
      colonIdx = fullIdx;
    }

    if (colonIdx === -1) {
      skippedCount++;
      continue;
    }

    const word = line.slice(0, colonIdx).trim();
    const reading = line.slice(colonIdx + 1).trim();

    if (!word || !reading) {
      skippedCount++;
      continue;
    }

    parsedBatchDictItems.push({ word, reading });
  }

  countSpan.textContent = parsedBatchDictItems.length;

  if (skippedCount > 0) {
    skipSpan.textContent = `※ ${skippedCount}行をスキップ（コロン区切りなし・空欄）`;
    skipSpan.style.display = "inline";
  } else {
    skipSpan.style.display = "none";
  }

  if (parsedBatchDictItems.length > 0) {
    previewContainer.style.display = "block";
    const previewRows = parsedBatchDictItems.slice(0, 100);
    tbody.innerHTML = previewRows.map(item => `
      <tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
        <td style="padding:4px 6px;font-weight:600;color:var(--text-main);word-break:break-all;">${escapeHtml(item.word)}</td>
        <td style="padding:4px 6px;color:#23a55a;word-break:break-all;">${escapeHtml(item.reading)}</td>
      </tr>
    `).join("");

    if (parsedBatchDictItems.length > 100) {
      tbody.innerHTML += `
        <tr>
          <td colspan="2" style="text-align:center;padding:6px;color:var(--text-muted);font-style:italic;">
            ... 他 ${parsedBatchDictItems.length - 100} 件
          </td>
        </tr>
      `;
    }

    submitBtn.disabled = false;
    submitBtn.textContent = `一括インポートを実行 (${parsedBatchDictItems.length}件)`;
  } else {
    previewContainer.style.display = "none";
    tbody.innerHTML = "";
    submitBtn.disabled = true;
    submitBtn.textContent = "一括インポートを実行 (0件)";
  }
}

async function executeDictBatchImport() {
  if (!currentGuildId) {
    showToast("サーバーが選択されていません", "error");
    return;
  }
  if (!parsedBatchDictItems || parsedBatchDictItems.length === 0) {
    showToast("インポート可能な単語がありません", "error");
    return;
  }

  const submitBtn = document.getElementById("dict-batch-submit-btn");
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = "インポート中...";
  }

  try {
    const res = await fetch("/api/dict/import_batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: currentGuildId,
        items: parsedBatchDictItems
      })
    });

    const data = await res.json();
    if (res.ok) {
      showToast(`一括インポートが完了しました！（${data.imported_count}件追加・更新、キャッシュ${data.purged_cache_count}件パージ）`, "success");
      closeDictBatchImportModal();
      await loadDictionary(currentGuildId);
    } else {
      showToast(data.error || "一括インポートに失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = `一括インポートを実行 (${parsedBatchDictItems.length}件)`;
    }
  }
}

// --- ギルドロール一覧 ---

async function loadGuildRoles(guildId) {
  if (!guildId) return;
  try {
    const res = await fetch(`/api/guilds/${guildId}/roles`);
    if (!res.ok) return;
    const data = await res.json();
    guildRoles = data.roles || [];

    const roleSelect = document.getElementById("cfg-admin-role");
    if (!roleSelect) return;

    // 現在選択されている値を保持
    const curVal = roleSelect.value;
    roleSelect.innerHTML = '<option value="0">ロール指定なし (管理者のみ許可)</option>';

    guildRoles.forEach(r => {
      const opt = document.createElement("option");
      opt.value = r.id;
      opt.textContent = `@${r.name}`;
      opt.style.color = r.color;
      roleSelect.appendChild(opt);
    });

    if (curVal) roleSelect.value = curVal;
  } catch (e) {
    console.error("Failed to load guild roles:", e);
  }
}

// --- ギルドチャンネル一覧 (VC / Text) & 自動参加ペアリング (稼働Bot数連動) ---

let currentGuildChannels = { voice: [], text: [] };
let cachedAutoJoinPairs = [];
let currentMaxAutoJoinPairs = 5;

function updateAutoJoinPairsHeader(maxPairs) {
  const count = Math.max(1, Math.min(10, maxPairs || 5));
  const labelEl = document.getElementById("auto-join-max-label");
  if (labelEl) labelEl.textContent = `最大${count}組`;

  const badgeEl = document.getElementById("auto-join-max-badge");
  if (badgeEl) badgeEl.textContent = `最大${count}VC同時`;

  const descEl = document.getElementById("auto-join-bots-desc");
  if (descEl) {
    descEl.textContent = count > 1 ? `1〜${count}号機` : `1号機`;
  }
}

async function loadGuildChannels(guildId) {
  if (!guildId) return;
  try {
    const res = await fetch(`/api/guilds/${guildId}/channels`);
    if (!res.ok) return;
    const data = await res.json();
    currentGuildChannels.voice = data.voice_channels || [];
    currentGuildChannels.text = data.text_channels || [];

    // ペアリングUIの再描画 (稼働Bot数連動)
    renderAutoJoinPairs(cachedAutoJoinPairs, currentMaxAutoJoinPairs);
  } catch (e) {
    console.error("Failed to load guild channels:", e);
  }
}

function renderAutoJoinPairs(pairs, maxPairs) {
  const container = document.getElementById("auto-join-pairs-list");
  if (!container) return;

  const count = typeof maxPairs === "number" ? Math.max(1, Math.min(10, maxPairs)) : (currentMaxAutoJoinPairs || 5);
  currentMaxAutoJoinPairs = count;
  updateAutoJoinPairsHeader(count);

  container.innerHTML = "";
  const voiceChannels = currentGuildChannels.voice || [];
  const textChannels = currentGuildChannels.text || [];

  for (let i = 0; i < count; i++) {
    const pair = (pairs && pairs[i]) ? pairs[i] : { vc_id: "0", text_id: "0" };
    const row = document.createElement("div");
    row.className = "auto-pair-row";
    row.dataset.index = i;
    row.style.cssText = "display:flex;align-items:center;gap:10px;background:rgba(0,0,0,0.25);padding:10px 14px;border-radius:8px;border:1px solid rgba(255,255,255,0.05);flex-wrap:wrap;";

    // ペア番号バッジ
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.style.cssText = "background:#4f545c;color:white;font-size:12px;font-weight:700;min-width:64px;text-align:center;padding:4px 8px;";
    badge.textContent = `ペア #${i + 1}`;
    row.appendChild(badge);

    // VC選択
    const vcWrapper = document.createElement("div");
    vcWrapper.style.cssText = "flex:1;min-width:180px;";
    const vcSelect = document.createElement("select");
    vcSelect.className = "form-input auto-pair-vc";
    vcSelect.dataset.index = i;
    vcSelect.style.cssText = "width:100%;font-size:13px;padding:6px 10px;";
    
    let vcHtml = '<option value="0">🔇 未設定 (無効)</option>';
    voiceChannels.forEach(ch => {
      const isSel = String(ch.id) === String(pair.vc_id);
      vcHtml += `<option value="${escapeHtml(ch.id)}" ${isSel ? 'selected' : ''}>🔊 ${escapeHtml(ch.name)}</option>`;
    });
    vcSelect.innerHTML = vcHtml;
    vcWrapper.appendChild(vcSelect);
    row.appendChild(vcWrapper);

    // 矢印
    const arrow = document.createElement("span");
    arrow.style.cssText = "color:var(--text-dim);font-size:15px;font-weight:700;";
    arrow.textContent = "➔";
    row.appendChild(arrow);

    // Text選択
    const textWrapper = document.createElement("div");
    textWrapper.style.cssText = "flex:1;min-width:180px;";
    const textSelect = document.createElement("select");
    textSelect.className = "form-input auto-pair-text";
    textSelect.dataset.index = i;
    textSelect.style.cssText = "width:100%;font-size:13px;padding:6px 10px;";

    let textHtml = '<option value="0">💬 未設定 (無効)</option>';
    textChannels.forEach(ch => {
      const isSel = String(ch.id) === String(pair.text_id);
      textHtml += `<option value="${escapeHtml(ch.id)}" ${isSel ? 'selected' : ''}># ${escapeHtml(ch.name)}</option>`;
    });
    textSelect.innerHTML = textHtml;
    textWrapper.appendChild(textSelect);
    row.appendChild(textWrapper);

    // クリアボタン
    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "btn btn-secondary";
    clearBtn.style.cssText = "padding:5px 10px;font-size:12px;background:rgba(255,255,255,0.08);color:var(--text-dim);border:none;border-radius:4px;cursor:pointer;";
    clearBtn.textContent = "解除";
    clearBtn.title = "このペアを未設定にする";
    clearBtn.onclick = () => clearAutoJoinPair(i);
    row.appendChild(clearBtn);

    container.appendChild(row);
  }
}

function clearAutoJoinPair(index) {
  const vcSelect = document.querySelector(`.auto-pair-vc[data-index="${index}"]`);
  const textSelect = document.querySelector(`.auto-pair-text[data-index="${index}"]`);
  if (vcSelect) vcSelect.value = "0";
  if (textSelect) textSelect.value = "0";
  if (cachedAutoJoinPairs && cachedAutoJoinPairs[index]) {
    cachedAutoJoinPairs[index] = { vc_id: "0", text_id: "0" };
  }
}
window.clearAutoJoinPair = clearAutoJoinPair;
window.renderAutoJoinPairs = renderAutoJoinPairs;

// --- ボット設定 (ギルド別) ---

async function loadBotConfig(guildId) {
  if (!guildId) return;
  try {
    const res = await fetch(`/api/config?guild_id=${guildId}`);
    if (res.status === 403) {
      updatePermissionsUI();
      return;
    }
    const cfg = await res.json();

    document.getElementById("cfg-max-len").value = cfg.max_text_length || 50;
    document.getElementById("cfg-dedup-sec").value = cfg.dedup_window_seconds || 5.0;
    document.getElementById("cfg-dedup-len").value = cfg.dedup_max_length || 10;
    
    // 自動参加ペアリング (稼働Bot数連動)
    if (typeof cfg.max_auto_join_pairs === "number") {
      currentMaxAutoJoinPairs = cfg.max_auto_join_pairs;
    } else if (typeof cfg.active_bot_count === "number") {
      currentMaxAutoJoinPairs = cfg.active_bot_count;
    }
    cachedAutoJoinPairs = cfg.auto_join_pairs || [];
    renderAutoJoinPairs(cachedAutoJoinPairs, currentMaxAutoJoinPairs);

    // 読み上げ除外プレフィックス
    renderIgnorePrefixesUI(cfg.ignore_prefixes || [";", "；"]);

    // 発言者名（サーバーネーム）読み上げ
    const readUserCheckbox = document.getElementById("cfg-read-username");
    if (readUserCheckbox) {
      readUserCheckbox.checked = !!cfg.read_username;
    }

    // VC入退室通知読み上げ
    const announceVCCheckbox = document.getElementById("cfg-announce-vc");
    if (announceVCCheckbox) {
      announceVCCheckbox.checked = !!cfg.announce_vc_events;
    }

    // 投機的フォールバック待機時間 (秒)
    const specTimeoutInput = document.getElementById("cfg-speculative-timeout");
    if (specTimeoutInput) {
      specTimeoutInput.value = (cfg.speculative_timeout_seconds !== undefined && cfg.speculative_timeout_seconds !== null) ? cfg.speculative_timeout_seconds : 1.5;
    }

    // 防災速報設定
    const disasterEnabled = document.getElementById("cfg-disaster-enabled");
    if (disasterEnabled) {
      disasterEnabled.checked = cfg.disaster_alert_enabled !== false;
    }
    const disasterMinScale = document.getElementById("cfg-disaster-min-scale");
    if (disasterMinScale) {
      disasterMinScale.value = String(cfg.disaster_min_scale || 30);
    }
    const disasterPref = document.getElementById("cfg-disaster-pref");
    if (disasterPref) {
      disasterPref.value = cfg.disaster_pref_filter || "";
    }
    const disasterWeather = document.getElementById("cfg-disaster-weather");
    if (disasterWeather) {
      disasterWeather.checked = cfg.disaster_alert_weather !== false;
    }
    const disasterVolume = document.getElementById("cfg-disaster-volume");
    if (disasterVolume) {
      const volPct = Math.round(parseFloat(cfg.disaster_volume !== undefined && cfg.disaster_volume !== null ? cfg.disaster_volume : 1.0) * 100);
      updateDisasterVolumeVal(volPct);
    }

    // 管理者ロール
    const roleSelect = document.getElementById("cfg-admin-role");
    if (roleSelect && cfg.admin_role_id) {
      roleSelect.value = String(cfg.admin_role_id);
    }

    // エラー連絡先 Discord ユーザーID (サーバー固有)
    const errorContactInput = document.getElementById("cfg-error-contact-user-id");
    if (errorContactInput) {
      errorContactInput.value = (cfg.error_contact_user_id && cfg.error_contact_user_id !== "0") ? String(cfg.error_contact_user_id) : "";
    }
    updateStandbyOneliner();
  } catch (e) {
    console.error("Failed to load bot config:", e);
  }
}

// --- 読み上げ除外プレフィックス UI制御 ---
let currentIgnorePrefixes = [";", "；"];

function renderIgnorePrefixesUI(prefixes) {
  currentIgnorePrefixes = Array.isArray(prefixes) ? [...prefixes] : [";", "；"];
  const container = document.getElementById("cfg-ignore-prefixes-tags");
  if (!container) return;

  if (currentIgnorePrefixes.length === 0) {
    container.innerHTML = '<span style="color:var(--text-muted);font-size:13px;font-style:italic;">(除外プレフィックスなし - 全てのメッセージが読み上げられます)</span>';
    return;
  }

  container.innerHTML = currentIgnorePrefixes.map((p, idx) => `
    <span class="badge" style="background:rgba(88,101,242,0.25); border:1px solid rgba(88,101,242,0.5); color:#fff; font-size:13px; padding:4px 8px; border-radius:4px; display:inline-flex; align-items:center; gap:6px;">
      <code>${escapeHtml(p)}</code>
      <button type="button" onclick="removeIgnorePrefixUI(${idx})" style="background:none; border:none; color:rgba(255,255,255,0.7); cursor:pointer; padding:0; line-height:1; font-size:14px;" title="削除">×</button>
    </span>
  `).join("");
}

function addIgnorePrefixFromInput() {
  const input = document.getElementById("cfg-ignore-prefix-input");
  if (!input) return;
  const val = input.value.trim();
  if (!val) return;

  const parts = val.split(/[,、\s]+/).filter(x => x.length > 0);
  for (const part of parts) {
    if (!currentIgnorePrefixes.includes(part)) {
      currentIgnorePrefixes.push(part.substring(0, 20));
    }
  }
  input.value = "";
  renderIgnorePrefixesUI(currentIgnorePrefixes);
}

function removeIgnorePrefixUI(idx) {
  if (idx >= 0 && idx < currentIgnorePrefixes.length) {
    currentIgnorePrefixes.splice(idx, 1);
    renderIgnorePrefixesUI(currentIgnorePrefixes);
  }
}

function resetIgnorePrefixesUI() {
  currentIgnorePrefixes = [";", "；"];
  renderIgnorePrefixesUI(currentIgnorePrefixes);
  showToast("除外プレフィックスをデフォルト（; と ；）にリセットしました（保存ボタンで確定）", "info");
}

async function saveBotConfig(e) {
  e.preventDefault();
  if (!currentGuildId) {
    showToast("サーバーが選択されていません", "error");
    return;
  }

  const maxLen = document.getElementById("cfg-max-len").value;
  const dedupSec = document.getElementById("cfg-dedup-sec").value;
  const dedupLen = document.getElementById("cfg-dedup-len").value;
  const adminRole = document.getElementById("cfg-admin-role")?.value || "0";
  const readUsername = document.getElementById("cfg-read-username")?.checked || false;
  const announceVCEvents = document.getElementById("cfg-announce-vc")?.checked || false;
  const specTimeout = parseFloat(document.getElementById("cfg-speculative-timeout")?.value || 1.5);

  const disasterEnabled = document.getElementById("cfg-disaster-enabled")?.checked ?? true;
  const disasterMinScale = parseInt(document.getElementById("cfg-disaster-min-scale")?.value || "30");
  const disasterPref = document.getElementById("cfg-disaster-pref")?.value?.trim() || "";
  const disasterWeather = document.getElementById("cfg-disaster-weather")?.checked ?? true;
  const disasterVolumeVal = parseInt(document.getElementById("cfg-disaster-volume")?.value || "100");
  const disasterVolume = Math.max(0.0, Math.min(2.0, disasterVolumeVal / 100.0));

  // 稼働Bot数連動のペアリングを抽出
  const pairs = [];
  const pairRows = document.querySelectorAll(".auto-pair-row");
  const countToExtract = pairRows.length > 0 ? pairRows.length : (currentMaxAutoJoinPairs || 5);
  for (let i = 0; i < countToExtract; i++) {
    const vcSelect = document.querySelector(`.auto-pair-vc[data-index="${i}"]`);
    const textSelect = document.querySelector(`.auto-pair-text[data-index="${i}"]`);
    const vcId = vcSelect ? vcSelect.value : "0";
    const textId = textSelect ? textSelect.value : "0";
    if (vcId && vcId !== "0" && textId && textId !== "0") {
      pairs.push({ vc_id: vcId, text_id: textId });
    }
  }

  const payload = {
    guild_id: currentGuildId,
    max_text_length: parseInt(maxLen),
    dedup_window_seconds: parseFloat(dedupSec),
    dedup_max_length: parseInt(dedupLen),
    ignore_prefixes: currentIgnorePrefixes,
    auto_join_pairs: pairs,
    auto_join_vc_id: pairs.length > 0 ? pairs[0].vc_id : "0",
    auto_join_text_channel_id: pairs.length > 0 ? pairs[0].text_id : "0",
    read_username: readUsername,
    announce_vc_events: announceVCEvents,
    speculative_timeout_seconds: specTimeout,
    disaster_alert_enabled: disasterEnabled,
    disaster_min_scale: disasterMinScale,
    disaster_pref_filter: disasterPref,
    disaster_alert_weather: disasterWeather,
    disaster_volume: disasterVolume,
    error_contact_user_id: document.getElementById("cfg-error-contact-user-id")?.value?.trim() || ""
  };

  // 管理者の場合のみ管理者ロール変更を含める
  if (currentGuildPerms.is_admin) {
    payload.admin_role_id = adminRole;
  }

  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (res.ok) {
      showToast("サーバー別ボット設定を保存しました！", "success");
      cachedAutoJoinPairs = pairs;
    } else {
      showToast(data.error || "設定の保存に失敗しました", "error");
    }
  } catch (err) {
    showToast("通信エラーが発生しました", "error");
  }
}

function updateDisasterVolumeVal(val) {
  const v = Math.max(0, Math.min(200, parseInt(val) || 100));
  const el = document.getElementById("cfg-disaster-volume-val");
  if (el) el.textContent = `${v}%`;
  const slider = document.getElementById("cfg-disaster-volume");
  if (slider && slider.value !== String(v)) slider.value = String(v);
}

function setDisasterVolumePreset(val) {
  updateDisasterVolumeVal(val);
}

async function triggerDisasterTest(type) {
  if (!currentGuildId) {
    showToast("サーバーが選択されていません", "error");
    return;
  }
  const typeNames = {
    earthquake: "緊急地震速報",
    tsunami: "津波警報",
    weather: "気象特別警報"
  };
  const label = typeNames[type] || type;
  try {
    showToast(`防災速報テスト（${label}）を送信中...`, "info");
    const res = await fetch("/api/disaster/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: type, guild_id: currentGuildId })
    });
    const data = await res.json();
    if (res.ok) {
      showToast(data.message || `テスト（${label}）を発信しました！`, "success");
    } else {
      showToast(data.error || "テスト発信に失敗しました", "error");
    }
  } catch (err) {
    console.error("Failed to trigger disaster test:", err);
    showToast("通信エラーが発生しました", "error");
  }
}

// --- システム & キャッシュ統計 (TTS無料枠利用状況メーター含む) ---

async function loadStats(guildId) {
  const gId = guildId || currentGuildId;
  const url = gId ? `/api/stats?guild_id=${encodeURIComponent(gId)}` : "/api/stats";
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (res.status === 403) {
      updatePermissionsUI();
      return;
    }
    const data = await res.json();

    // 1. キャッシュ使用量 (Cloud 10GB & VOICEVOX 20GB)
    const cache = data.cache || {};
    const cachePct = cache.max_bytes > 0 ? Math.min(100, (cache.used_bytes / cache.max_bytes) * 100) : 0;
    const fill = document.getElementById("cache-progress-fill");
    if (fill) fill.style.width = `${cachePct}%`;

    const usedEl = document.getElementById("cache-used-text");
    if (usedEl) usedEl.textContent = `${cache.used_mb || 0} MB (${cache.used_gb || 0} GB)`;
    const maxEl = document.getElementById("cache-max-text");
    if (maxEl) maxEl.textContent = `${cache.max_gb || 10.0} GB (上限)`;
    const filesEl = document.getElementById("cache-files-text");
    if (filesEl) filesEl.textContent = `${cache.file_count || 0} 個`;

    // VOICEVOX 20GB 専用キャッシュ
    const vvCache = data.cache_voicevox || {};
    const vvPct = vvCache.max_bytes > 0 ? Math.min(100, (vvCache.used_bytes / vvCache.max_bytes) * 100) : 0;
    const vvFill = document.getElementById("vv-cache-progress-fill");
    if (vvFill) vvFill.style.width = `${vvPct}%`;

    const vvUsedEl = document.getElementById("vv-cache-used-text");
    if (vvUsedEl) vvUsedEl.textContent = `${vvCache.used_mb || 0} MB (${vvCache.used_gb || 0} GB)`;
    const vvMaxEl = document.getElementById("vv-cache-max-text");
    if (vvMaxEl) vvMaxEl.textContent = `${vvCache.max_gb || 20.0} GB (上限)`;
    const vvFilesEl = document.getElementById("vv-cache-files-text");
    if (vvFilesEl) vvFilesEl.textContent = `${vvCache.file_count || 0} 個`;

    // 2. クラウドTTS利用状況 (Azure & Google & VOICEVOX)
    if (data.tts_usage) {
      renderTtsUsage(data.tts_usage);
    }

    // 2.5 稼働中Botプール一覧
    if (data.bot_pool) {
      renderBotPool(data.bot_pool);
    }

    if (data.latest_agent_version) {
      window.latestAgentVersion = data.latest_agent_version;
    }

    // 2.8 分散コンピュートノード一覧
    if (data.nodes) {
      renderWorkerNodes(data.nodes);
    }

    // 3. 接続ギルド一覧
    const guildsList = document.getElementById("guilds-list-container");
    if (guildsList && data.bot.guilds) {
      guildsList.innerHTML = "";
      data.bot.guilds.forEach(g => {
        const item = document.createElement("div");
        item.style.padding = "10px 14px";
        item.style.background = "rgba(0,0,0,0.2)";
        item.style.borderRadius = "8px";
        item.style.marginBottom = "8px";
        item.style.display = "flex";
        item.style.justifyContent = "space-between";
        item.style.alignItems = "center";

        const vcStatus = g.connected_vc 
          ? `<span class="badge" style="background:#23a55a;color:white;">🔊 ${escapeHtml(g.connected_vc)} に接続中</span>`
          : `<span style="font-size:12px;color:var(--text-dim);">VC未接続</span>`;

        item.innerHTML = `
          <div>
            <strong>${escapeHtml(g.name)}</strong>
            <span style="font-size:12px;color:var(--text-muted);margin-left:8px;">(メンバー: ${g.member_count}人)</span>
          </div>
          <div>${vcStatus}</div>
        `;
        guildsList.appendChild(item);
      });
    }
  } catch (e) {
    console.error("Failed to load stats:", e);
  }
}

function renderBotPool(botPool) {
  const container = document.getElementById("bot-pool-container");
  const countBadge = document.getElementById("bot-pool-count-badge");
  if (!container) return;

  if (countBadge) {
    const readyCount = botPool.filter(b => b.is_ready).length;
    countBadge.textContent = `${readyCount}/${botPool.length}台 稼働中`;
    countBadge.style.background = readyCount > 0 ? "#23a55a" : "#ed4245";

    // 稼働Bot数の変化を自動参加ペア設定の上限に同期 (最低1、最大10)
    if (readyCount > 0 && readyCount !== currentMaxAutoJoinPairs) {
      currentMaxAutoJoinPairs = Math.max(1, Math.min(10, readyCount));
      const autoJoinList = document.getElementById("auto-join-pairs-list");
      if (autoJoinList && autoJoinList.children.length > 0) {
        renderAutoJoinPairs(cachedAutoJoinPairs, currentMaxAutoJoinPairs);
      } else {
        updateAutoJoinPairsHeader(currentMaxAutoJoinPairs);
      }
    }
  }

  container.innerHTML = "";
  if (!botPool || botPool.length === 0) {
    container.innerHTML = '<div style="color:var(--text-dim);font-size:13px;">登録されているBotがありません</div>';
    return;
  }

  botPool.forEach(bot => {
    const card = document.createElement("div");
    card.style.padding = "10px 12px";
    card.style.background = "rgba(0,0,0,0.25)";
    card.style.border = bot.is_ready ? "1px solid rgba(88,101,242,0.3)" : "1px solid rgba(255,255,255,0.05)";
    card.style.borderRadius = "8px";
    card.style.fontSize = "13px";

    const isMain = bot.bot_index === 1;
    const roleBadge = isMain 
      ? '<span class="badge" style="background:#5865f2;color:white;font-size:11px;">1号機 (メイン/コマンド)</span>'
      : `<span class="badge" style="background:#4f545c;color:white;font-size:11px;">${bot.bot_index}号機 (ワーカー)</span>`;

    let vcInfo = '<span style="color:var(--text-dim);font-size:12px;">待機中 (VC未接続)</span>';
    if (bot.connected_vcs && bot.connected_vcs.length > 0) {
      const vcList = bot.connected_vcs.map(v => 
        `<div style="color:#23a55a;font-weight:600;font-size:12px;margin-top:2px;">🔊 ${escapeHtml(v.guild_name)}: ${escapeHtml(v.voice_channel_name)}${v.text_channel_name ? ` ( #${escapeHtml(v.text_channel_name)} )` : ''}</div>`
      ).join("");
      vcInfo = vcList;
    }

    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
        <span style="font-weight:700;color:var(--text-main);">${escapeHtml(bot.user)}</span>
        ${bot.is_ready ? '<span style="color:#23a55a;font-size:12px;">● オンライン</span>' : '<span style="color:#ed4245;font-size:12px;">○ オフライン</span>'}
      </div>
      <div style="margin-bottom:6px;">${roleBadge}</div>
      <div style="background:rgba(0,0,0,0.2);padding:6px 8px;border-radius:6px;">${vcInfo}</div>
    `;
    container.appendChild(card);
  });
}

function renderWorkerNodes(nodesData) {
  const container = document.getElementById("worker-nodes-container");
  if (!container) return;

  const latestBadge = document.getElementById("worker-latest-version-badge");
  if (latestBadge) {
    let maxVer = window.latestAgentVersion || "3.13.2";
    for (const [engine, nodes] of Object.entries(nodesData || {})) {
      if (!Array.isArray(nodes)) continue;
      nodes.forEach(n => {
        if (n.agent_version && n.agent_version > maxVer) maxVer = n.agent_version;
      });
    }
    latestBadge.textContent = `最新 Agent v${maxVer} (自律同期対応)`;
  }

  container.innerHTML = "";

  // エンジン別ノードをマージして「物理マシン」単位に集約
  const allNodes = [];
  const seenHostIds = new Map(); // baseId → index in allNodes
  for (const [engine, nodes] of Object.entries(nodesData || {})) {
    if (!Array.isArray(nodes)) continue;
    nodes.forEach(n => {
      // IDは "{node_id}_{engine_name}" 形式 → baseIdを取り出す
      const baseId = n.id.replace(/_voicevox$|_sharevox$|_coeiroink$/, "");
      if (!seenHostIds.has(baseId)) {
        seenHostIds.set(baseId, allNodes.length);
        allNodes.push({ ...n, id: baseId, engines: [{ name: engine, ...n }] });
      } else {
        const idx = seenHostIds.get(baseId);
        allNodes[idx].engines.push({ name: engine, ...n });
        // specs/resourcesは最新のものを使う
        if (n.specs && Object.keys(n.specs).length > 0) allNodes[idx].specs = n.specs;
      }
    });
  }

  if (allNodes.length === 0) {
    container.innerHTML = '<div style="color:var(--text-dim);font-size:13px;grid-column:1/-1;padding:20px;text-align:center;">登録されているWorkerノードがありません<br><small style="opacity:.6">セットアップスクリプトを実行するとここに表示されます</small></div>';
    return;
  }

  allNodes.forEach(node => {
    try {
      const specs = node.specs || {};
      const resources = specs.resources || {};
      const isMaster = node.id === "local_master" || node.id.includes("local_master");
      const rawHostname = specs.hostname || node.hostname || node.id;
      const hasGpu = specs.gpu || false;
      const gpuName = specs.gpu_name || "";
      const agentVersion = specs.agent_version || "";
      let nodeIp = "";
      if (node.url) {
        try {
          nodeIp = new URL(node.url).hostname;
        } catch (e) {
          nodeIp = (node.url.match(/https?:\/\/([^:/]+)/) || [])[1] || "";
        }
      }

    let resolvedHostname = rawHostname;
    const cleanId = String(rawHostname || '').replace(/^worker_/, '').trim();
    const isHexContainerId = /^[0-9a-f]{12}$/i.test(cleanId);

    if (isHexContainerId || cleanId.length === 12) {
      if (specs.gpu_name) {
        const cleanGpu = String(specs.gpu_name).replace(/NVIDIA (GeForce )?/i, '').trim();
        resolvedHostname = `Worker (${cleanGpu})`;
      } else if (specs.os) {
        resolvedHostname = `Worker (${specs.os})`;
      } else {
        resolvedHostname = `Worker (${cleanId.substring(0, 6)})`;
      }
    } else if (cleanId) {
      resolvedHostname = cleanId;
    }

    const isVolunteer = Boolean(node.is_volunteer || (node.engines || []).some(e => e.is_volunteer) || cleanId.startsWith('volunteer_') || (node.url && node.url.startsWith('ws://')));

    // アイコン
    const icon = isMaster ? "🏢" : (isVolunteer ? "🤝" : (hasGpu ? "🎮" : "🖥️"));
    let nodeName = isMaster ? "MasterDebian (自機)" : resolvedHostname;
    if (isVolunteer) {
      nodeName = `${resolvedHostname} <span style="background:rgba(88,101,242,0.25);border:1px solid rgba(88,101,242,0.5);color:#50e6ff;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;">🤝 ボランティア</span>`;
    }

    // 1. Agent のオンライン/オフライン判定 (ハートビート疎通)
    const hbAgo = node.last_heartbeat_ago;
    const isAgentOnline = isMaster || (node.agent_online !== undefined ? Boolean(node.agent_online) : (hbAgo != null && hbAgo < 60));
    const agentBadge = isVolunteer
      ? '<span class="badge" style="background:rgba(88,101,242,0.2);color:#50e6ff;border:1px solid rgba(88,101,242,0.4);font-size:10px;font-weight:700;">⚡ WSS 常時接続</span>'
      : (isAgentOnline
        ? '<span class="badge" style="background:rgba(35,165,90,0.18);color:#23a55a;border:1px solid rgba(35,165,90,0.35);font-size:10px;font-weight:700;">🟢 Agent 稼働</span>'
        : `<span class="badge" style="background:rgba(240,71,71,0.18);color:#f04747;border:1px solid rgba(240,71,71,0.35);font-size:10px;font-weight:700;">🔴 Agent 不通 (${hbAgo != null ? Math.round(hbAgo)+'s前' : '不明'})</span>`);

    // 2. エンジン全体の稼働状況判定
    const engines = node.engines || [];
    let runningEnginesCount = 0;
    let stoppedEnginesCount = 0;
    let unhealthyEnginesCount = 0;

    engines.forEach(e => {
      const isRun = e.engine_running !== false && e.specs?.engine_running !== false;
      const isAvail = e.is_available ?? e.is_healthy;
      if (isRun && isAvail) {
        runningEnginesCount++;
      } else if (!isRun) {
        stoppedEnginesCount++;
      } else {
        unhealthyEnginesCount++;
      }
    });

    let engineSummaryBadge;
    if (node.draining) {
      engineSummaryBadge = '<span class="badge" style="background:#faa61a;color:#1e1f22;font-size:10px;font-weight:700;">⚠️ ドレイン中</span>';
    } else if (!node.enabled) {
      engineSummaryBadge = '<span class="badge" style="background:#747f8d;color:white;font-size:10px;">無効</span>';
    } else if (!isAgentOnline) {
      engineSummaryBadge = '<span class="badge" style="background:#f04747;color:white;font-size:10px;font-weight:700;">⚠️ ホスト不通</span>';
    } else if (runningEnginesCount === engines.length && engines.length > 0) {
      engineSummaryBadge = '<span class="badge" style="background:#23a55a;color:white;font-size:10px;font-weight:700;">● 全エンジン稼働</span>';
    } else if (runningEnginesCount > 0) {
      engineSummaryBadge = `<span class="badge" style="background:#faa61a;color:#1e1f22;font-size:10px;font-weight:700;">⚠️ 一部稼働 (${runningEnginesCount}/${engines.length})</span>`;
    } else {
      engineSummaryBadge = '<span class="badge" style="background:rgba(250,166,26,0.2);color:#faa61a;border:1px solid rgba(250,166,26,0.5);font-size:10px;font-weight:700;">⏸️ 全エンジン停止中</span>';
    }

    // リソースバー生成ヘルパー
    function makeBar(label, percent, color, sublabel) {
      if (percent == null) return '';
      const pct = Math.min(100, Math.max(0, percent));
      const barColor = pct > 85 ? '#f04747' : pct > 65 ? '#faa61a' : color;
      return `
        <div style="margin-bottom:6px;">
          <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-dim);margin-bottom:2px;">
            <span>${label}</span>
            <span style="color:${barColor};font-weight:600;">${pct.toFixed(1)}%${sublabel ? ' <span style="color:var(--text-dim);font-weight:400;">('+sublabel+')</span>' : ''}</span>
          </div>
          <div style="height:5px;background:rgba(255,255,255,0.08);border-radius:3px;overflow:hidden;">
            <div style="height:100%;width:${pct}%;background:${barColor};border-radius:3px;transition:width 0.5s ease;"></div>
          </div>
        </div>`;
    }

    // CPU/メモリ/GPU バー
    const cpuBar  = makeBar("CPU", resources.cpu_percent, "#5865f2", null);
    const memUsed = resources.mem_used_gb != null ? `${resources.mem_used_gb}/${resources.mem_total_gb}GB` : null;
    const memBar  = makeBar("RAM", resources.mem_percent, "#00b0f4", memUsed);
    const gpuBar  = hasGpu && resources.gpu_util_percent != null
      ? makeBar(`GPU (${gpuName || 'GPU'})`, resources.gpu_util_percent, "#a8ff78",
          resources.gpu_mem_used_mb != null
            ? `VRAM ${Math.round(resources.gpu_mem_used_mb/1024*10)/10}/${Math.round(resources.gpu_mem_total_mb/1024*10)/10}GB`
            : null)
      : '';
    const gpuTemp = hasGpu && resources.gpu_temp_celsius != null
      ? `<span style="font-size:10px;color:${resources.gpu_temp_celsius > 80 ? '#f04747' : 'var(--text-dim)'};">🌡️ ${resources.gpu_temp_celsius}°C</span>`
      : '';
    const hasResourceData = resources.cpu_percent != null || resources.mem_percent != null;
    const resourceSection = hasResourceData
      ? `<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.06);">
           ${cpuBar}${memBar}${gpuBar}
           ${gpuTemp}
         </div>`
      : (node.is_healthy && !isMaster
          ? `<div style="font-size:10px;color:var(--text-dim);margin-top:6px;">リソース情報待機中... (次のハートビートで更新)</div>`
          : '');

    // エンジン別稼働状況 & セマフォスロット (視覚的ステータスを大幅強化)
    const engineBadges = engines.map(e => {
      const isRun = e.engine_running !== false && e.specs?.engine_running !== false;
      const isAvail = e.is_available ?? e.is_healthy;
      const activeCount = e.active_count || 0;
      const maxC = e.max_concurrent || 2;

      let badgeBg, badgeBorder, badgeColor, dot, statusText;
      if (isRun && isAvail) {
        badgeBg = 'rgba(35,165,90,0.16)';
        badgeBorder = 'rgba(35,165,90,0.4)';
        badgeColor = '#a8ff78';
        dot = '●';
        statusText = '稼働中';
      } else if (!isRun) {
        badgeBg = 'rgba(250,166,26,0.15)';
        badgeBorder = 'rgba(250,166,26,0.45)';
        badgeColor = '#faa61a';
        dot = '○';
        statusText = '停止中';
      } else {
        badgeBg = 'rgba(240,71,71,0.15)';
        badgeBorder = 'rgba(240,71,71,0.45)';
        badgeColor = '#ff7878';
        dot = '⚠️';
        statusText = '異常/隔離';
      }

      const activeBadge = activeCount > 0
        ? ` <span style="background:rgba(80,230,255,0.25);color:#50e6ff;padding:0 4px;border-radius:3px;font-weight:700;">${activeCount}/${maxC}</span>`
        : ` <span style="color:var(--text-dim);font-size:9px;">(${maxC}並列)</span>`;

      return `<span style="background:${badgeBg};border:1px solid ${badgeBorder};color:${badgeColor};padding:2px 7px;border-radius:5px;font-size:10px;display:inline-flex;align-items:center;gap:3px;margin-right:5px;margin-bottom:4px;" title="${escapeHtml(e.name)}: ${statusText}">` +
        `<strong style="font-weight:700;">${dot} ${escapeHtml(e.name)}</strong>` +
        `<span style="opacity:0.85;font-size:9px;">[${statusText}]</span>` +
        `${activeBadge}</span>`;
    }).join("");

    // 統計
    const totalReqs = (node.engines || []).reduce((s, e) => s + (e.total_requests || 0), 0);
    const failedReqs = (node.engines || []).reduce((s, e) => s + (e.failed_requests || 0), 0);
    const successReqs = Math.max(0, totalReqs - failedReqs);
    const successRate = totalReqs > 0 ? ((successReqs / totalReqs) * 100).toFixed(1) : "100.0";
    const successBadge = totalReqs > 0
      ? `<span title="リクエスト成功率 (${successReqs}/${totalReqs}件)" style="color:${failedReqs > 0 ? '#faa61a' : '#23a55a'};font-weight:700;">🎯 ${successRate}%</span>`
      : `<span style="color:var(--text-dim);">🎯 --%</span>`;
    const lastLatency = node.last_latency_ms ? `${node.last_latency_ms}ms` : '--';
    const priorityLabel = node.priority === 1
      ? '<span style="color:#a8ff78;font-weight:600;">GPU 最優先</span>'
      : (isVolunteer ? '<span style="color:#50e6ff;font-weight:600;">ボランティア</span>' : '通常 CPU');

    // セマフォ合計稼働スロット
    const totalActive = (node.engines || []).reduce((s, e) => s + (e.active_count || 0), 0);
    const maxConcurrent = node.max_concurrent || 2;
    const semSlotLabel = totalActive > 0
      ? `<span style="background:rgba(80,230,255,0.2);color:#50e6ff;padding:1px 6px;border-radius:4px;font-weight:700;">実行中: ${totalActive} / ${maxConcurrent}並列</span>`
      : `<span>上限: ${maxConcurrent}並列</span>`;

    // アクションボタン (再起動・完全再構成・キック)
    let actionButtons = '';
    if (!isMaster && !isVolunteer && nodeIp) {
      const restartBtn = `<button onclick="triggerWorkerRestart('${escapeHtml(nodeIp)}', this)" 
           title="ワーカーの全TTSエンジンおよびAgentコンテナを再起動します (ドレイン・待機あり)"
           style="font-size:10px;padding:3px 8px;border-radius:5px;border:1px solid rgba(250,166,26,0.5);
                  background:rgba(250,166,26,0.15);color:#ffcc66;cursor:pointer;font-weight:600;
                  transition:all 0.2s;"
           onmouseover="this.style.background='rgba(250,166,26,0.4)';this.style.color='white'"
           onmouseout="this.style.background='rgba(250,166,26,0.15)';this.style.color='#ffcc66'">
           ⚡ 再起動
         </button>`;
      const updateBtn = node.is_healthy
        ? `<button onclick="triggerWorkerUpdate('${escapeHtml(nodeIp)}', this, true)" 
             title="Masterから最新の構成ファイル・モデルを自動取得し、コンテナを再ビルド・再起動します"
             style="font-size:10px;padding:3px 8px;border-radius:5px;border:1px solid rgba(88,101,242,0.4);
                    background:rgba(88,101,242,0.15);color:#50e6ff;cursor:pointer;font-weight:600;
                    transition:all 0.2s;"
             onmouseover="this.style.background='rgba(88,101,242,0.4)';this.style.color='white'"
             onmouseout="this.style.background='rgba(88,101,242,0.15)';this.style.color='#50e6ff'">
             🔄 完全再構成
           </button>`
        : '';
      actionButtons = `${restartBtn} ${updateBtn}`;
    }

    if (!isMaster) {
      const targetParam = node.id || nodeIp;
      const kickBtn = `<button onclick="kickWorkerNode('${escapeHtml(targetParam)}', '${escapeHtml(nodeIp)}', this)" 
           title="このWorkerノードを切断・除外します"
           style="font-size:10px;padding:3px 8px;border-radius:5px;border:1px solid rgba(231,76,60,0.4);
                  background:rgba(231,76,60,0.15);color:#ff6b6b;cursor:pointer;font-weight:600;
                  transition:all 0.2s;"
           onmouseover="this.style.background='rgba(231,76,60,0.4)';this.style.color='white'"
           onmouseout="this.style.background='rgba(231,76,60,0.15)';this.style.color='#ff6b6b'">
           🔌 切断
         </button>`;
      actionButtons = `${actionButtons} ${kickBtn}`.trim();
    }

    // レイテンシトラッカーからの実測平均推論ms
    const workerLatencyStats = (typeof latestMetricsData !== 'undefined' && latestMetricsData?.latency?.workers) ? latestMetricsData.latency.workers : {};
    let realSynthLabel = lastLatency;
    for (const [wLbl, wStat] of Object.entries(workerLatencyStats)) {
      if ((resolvedHostname && wLbl.includes(resolvedHostname)) || (rawHostname && wLbl.includes(rawHostname)) || (node.id && wLbl.includes(node.id)) || (isMaster && (wLbl.includes("Master") || wLbl.includes("local")))) {
        realSynthLabel = `<span style="color:#50e6ff;font-weight:700;" title="直近実測TTS合成速度">${wStat.avg_synthesis_ms}ms</span>`;
        break;
      }
    }

    // COEIROINK RAMDisk 常駐モデル一覧の可視化
    const hasCoeiroink = (node.engines || []).some(e => e.name === 'coeiroink');
    let ramModels = specs.ram_models || specs.active_models || [];
    if (!ramModels || ramModels.length === 0) {
      for (const e of (node.engines || [])) {
        if (e.specs?.ram_models && e.specs.ram_models.length > 0) {
          ramModels = e.specs.ram_models;
          break;
        }
      }
    }

    let coeiroinkRamSection = '';
    if (hasCoeiroink) {
      // ディレクトリ名とUUIDの重複を排除し、一意なモデル情報に集約
      const uniqueModelsMap = new Map();
      (Array.isArray(ramModels) ? ramModels : []).forEach(m => {
        if (!m) return;
        const str = String(m).trim();
        let key = str;
        let label = str;
        let isPinned = false;

        if (str.includes('ginga') || str.includes('d312d0fb')) {
          key = 'ginga';
          label = '銀河 (v2.2.0)';
          isPinned = true;
        } else if (str.includes('lilin') || str.includes('cb11bdbd')) {
          key = 'lilin';
          label = 'リリン (v2.1.0)';
          isPinned = true;
        } else if (str.includes('kanae') || str.includes('d41bcbd9')) {
          key = 'kanae';
          label = 'カナエ (v2.1.0)';
          isPinned = true;
        } else {
          const parts = str.split('_');
          const clean = parts[parts.length - 1];
          key = clean.toLowerCase();
          label = clean;
        }

        if (!uniqueModelsMap.has(key)) {
          uniqueModelsMap.set(key, { label, isPinned, original: str });
        }
      });

      const uniqueModels = Array.from(uniqueModelsMap.values());

      if (uniqueModels.length > 0) {
        const chips = uniqueModels.map(item => {
          const icon = item.isPinned ? '📌' : '💾';
          return `<span style="background:rgba(80,230,255,0.12);border:1px solid rgba(80,230,255,0.25);color:#50e6ff;padding:1px 5px;border-radius:4px;font-size:10px;white-space:nowrap;" title="${escapeHtml(item.original)}">${icon} ${escapeHtml(item.label)}</span>`;
        }).join(" ");

        coeiroinkRamSection = `
          <div style="font-size:11px;margin-top:4px;padding:4px 8px;background:rgba(0,0,0,0.25);border-radius:6px;border:1px solid rgba(80,230,255,0.15);">
            <div style="display:flex;justify-content:space-between;color:var(--text-dim);margin-bottom:3px;font-size:10px;">
              <span>⚡ COEIROINK RAMDisk 常駐 (${uniqueModels.length}件):</span>
              <span style="color:#23a55a;font-weight:600;">Tier 1/2 高速</span>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:4px;">${chips}</div>
          </div>
        `;
      } else if (node.is_healthy) {
        coeiroinkRamSection = `
          <div style="font-size:11px;margin-top:4px;padding:4px 8px;background:rgba(240,71,71,0.12);border-radius:6px;border:1px solid rgba(240,71,71,0.3);">
            <div style="display:flex;align-items:center;gap:6px;color:#ff7878;font-size:10px;font-weight:600;">
              <span>⚠️ COEIROINK RAMDisk 常駐: なし (Tier 3 低速フォールバック中)</span>
            </div>
          </div>
        `;
      }
    }

    // Agent生存中かつ一部/全エンジン停止時のアシスト通知
    const engineNotice = (isAgentOnline && stoppedEnginesCount > 0)
      ? `<div style="font-size:10px;color:#faa61a;background:rgba(250,166,26,0.1);border:1px solid rgba(250,166,26,0.3);padding:4px 8px;border-radius:5px;display:flex;align-items:center;gap:5px;margin-top:2px;">
           <span>ℹ️ Agent はオンラインですが、${stoppedEnginesCount === engines.length ? '全TTSエンジンが停止中' : '一部エンジンが停止中'}です (スリープ待機またはオフライン)。</span>
         </div>`
      : '';

    const card = document.createElement("div");
    const borderColor = node.draining
      ? 'rgba(250,166,26,0.6)'
      : !isAgentOnline
        ? 'rgba(240,71,71,0.45)'
        : (runningEnginesCount === engines.length && engines.length > 0)
          ? 'rgba(88,101,242,0.4)'
          : 'rgba(250,166,26,0.4)';

    card.style.cssText = `
      background:rgba(0,0,0,0.28);
      padding:14px 16px;
      border-radius:10px;
      border:1px solid ${borderColor};
      display:flex;flex-direction:column;gap:6px;
      transition:border-color 0.3s;
    `;

    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
        <div style="font-weight:700;font-size:13px;display:flex;align-items:center;gap:6px;">
          <span>${icon}</span>
          <span>${escapeHtml(nodeName)}</span>
          ${agentVersion ? `<span style="font-size:9px;color:var(--text-dim);background:rgba(255,255,255,0.06);padding:1px 5px;border-radius:4px;">v${escapeHtml(agentVersion)}</span>` : ''}
        </div>
        <div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;">
          ${agentBadge}
          ${engineSummaryBadge}
          ${actionButtons}
        </div>
      </div>
      ${nodeIp ? `<div style="font-family:monospace;font-size:10px;color:var(--text-dim);background:rgba(0,0,0,0.2);padding:3px 8px;border-radius:4px;">${escapeHtml(node.url || '')}</div>` : ''}
      <div style="display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--text-dim);">
        <span>優先度: ${priorityLabel}</span>
        ${semSlotLabel}
      </div>
      ${resourceSection}
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim);border-top:1px solid rgba(255,255,255,0.05);padding-top:6px;margin-top:2px;">
        <span>累計: <strong>${totalReqs}</strong>件</span>
        <span>成功率: ${successBadge}</span>
        <span>実測推論: <strong>${realSynthLabel}</strong></span>
        <span>HB: ${node.last_heartbeat_ago != null ? Math.round(node.last_heartbeat_ago)+'s前' : '--'}</span>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:2px;margin-top:2px;">${engineBadges}</div>
      ${engineNotice}
      ${coeiroinkRamSection}
    `;
    container.appendChild(card);
    } catch (err) {
      console.error("Worker node render error:", err);
    }
  });
}

// システムリソース ノード切り替え
window.currentMetricsNode = 'master';
window.switchMetricsNode = function(nodeKey) {
  window.currentMetricsNode = nodeKey;
  const tabs = document.querySelectorAll(".metrics-node-tab");
  tabs.forEach(t => {
    t.classList.remove("active");
    t.classList.add("btn-secondary");
    t.style.background = "";
    t.style.color = "";
  });
  const activeTab = document.getElementById(`metrics-tab-${nodeKey}`);
  if (activeTab) {
    activeTab.classList.add("active");
    activeTab.classList.remove("btn-secondary");
    activeTab.style.background = "#5865f2";
    activeTab.style.color = "white";
  }
  // タイトル表示の更新
  const cpuTitle = document.getElementById("metrics-cpu-title");
  const memTitle = document.getElementById("metrics-mem-title");
  const targetName = nodeKey === 'master' ? 'Primary Node (Master)' : (window._workerNodeLabels?.[nodeKey] || nodeKey);
  if (cpuTitle) cpuTitle.textContent = `🔥 ${targetName} CPU使用率推移 (%)`;
  if (memTitle) memTitle.textContent = `💾 ${targetName} 実質メモリ / VRAM推移`;

  // グラフ再描画
  if (typeof latestMetricsData !== 'undefined' && latestMetricsData) {
    renderSystemMetricsUI(latestMetricsData);
  } else if (typeof fetchSystemMetrics === 'function') {
    fetchSystemMetrics();
  }
};
window.doSwitchMetricsNode = window.switchMetricsNode;
if (window._pendingMetricsNode) {
  const pending = window._pendingMetricsNode;
  delete window._pendingMetricsNode;
  window.switchMetricsNode(pending);
}

async function triggerWorkerUpdate(workerIp, btn, full = true) {
  const orig = btn.innerHTML;
  btn.innerHTML = "⏳ 指示送信中...";
  btn.disabled = true;
  try {
    const resp = await fetch('/api/worker/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip: workerIp, agent_port: 50030, full: full, update_agent: true, update_engines: true })
    });
    const data = await resp.json();
    if (data.success) {
      btn.innerHTML = "✅ 自動構築開始";
      btn.style.background = "rgba(35,165,90,0.3)";
      btn.style.color = "#a8ff78";
      if (typeof showToast === 'function') {
        showToast(`⚡ Worker (${workerIp}) へ完全再構成を指示しました。バックグラウンドでビルド・再起動が進行します。`, 'success');
      }
      setTimeout(() => { btn.innerHTML = orig; btn.style.background = ""; btn.style.color = ""; btn.disabled = false; }, 6000);
    } else {
      btn.innerHTML = "❌ 失敗";
      btn.style.background = "rgba(240,71,71,0.3)";
      btn.style.color = "#f04747";
      setTimeout(() => { btn.innerHTML = orig; btn.style.background = ""; btn.style.color = ""; btn.disabled = false; }, 4000);
    }
  } catch(e) {
    btn.innerHTML = "❌ エラー";
    setTimeout(() => { btn.innerHTML = orig; btn.style.background = ""; btn.style.color = ""; btn.disabled = false; }, 4000);
  }
}

// ===== ローリング更新 / ローリング再起動 UI 管理 =====
window._rollingPollInterval = null;

async function triggerRollingJob(action) {
  const isRestart = action === 'restart';
  const label = isRestart ? '全ワーカー再起動' : '全ワーカー一括更新';
  const confirmMsg = isRestart
    ? `全ワーカーのTTSエンジンおよびAgentコンテナを1台ずつ順次再起動しますか？\n\n・各ノードは再起動前に自動ドレイン（割当停止）され、通話中リクエストは稼働中の他ノードへ自動退避されます。\n・エンジンの健全化を確認してから次のノードへ移行します。`
    : `全ワーカーノードを1台ずつ順次ローリング更新（完全再構成）しますか？\n\n・通話中リクエストへの影響を防ぐため、1台ずつドレイン（推論隔離）して更新します。\n・各ノードの復帰とエンジン健全化を確認してから次のノードへ移行します。`;

  if (!confirm(confirmMsg)) return;

  const btnId = isRestart ? "btn-restart-all-workers" : "btn-update-all-workers";
  const btn = document.getElementById(btnId);
  const origText = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = "⏳ 指示送信中...";
  }

  try {
    const resp = await fetch('/api/worker/rolling', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, full: true })
    });
    const data = await resp.json();
    if (data.success) {
      if (typeof showToast === 'function') {
        showToast(`⚡ ${label}（ローリング方式）を開始しました。対象: ${data.target_count} 台`, 'success');
      }
      openRollingProgressModal(action);
    } else {
      alert(data.error || `${label}の開始に失敗しました。`);
    }
  } catch (err) {
    alert(`通信エラー: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origText;
    }
  }
}

async function triggerWorkerRestart(workerIp, btn) {
  if (!confirm(`ワーカー (${workerIp}) の全TTSエンジンおよびAgentコンテナを再起動しますか？\n\n・実行中リクエストの完了を待機後、一時的にドレイン（割当停止）され他ノードへ自動退避されます。\n・エンジンの健全化を確認してからドレインが解除されます。`)) {
    return;
  }
  const orig = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = "⏳ 指示中...";
  }
  try {
    const resp = await fetch('/api/worker/restart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip: workerIp })
    });
    const data = await resp.json();
    if (data.success) {
      if (typeof showToast === 'function') {
        showToast(`⚡ Worker (${workerIp}) の安全再起動を開始しました。`, 'info');
      }
      openRollingProgressModal('restart');
    } else {
      alert(data.error || "再起動指示に失敗しました。");
    }
  } catch (err) {
    alert(`通信エラー: ${err.message}`);
  } finally {
    if (btn) {
      setTimeout(() => {
        btn.disabled = false;
        btn.innerHTML = orig;
      }, 3000);
    }
  }
}

function openRollingProgressModal(action) {
  const modal = document.getElementById("rolling-progress-modal");
  if (!modal) return;
  modal.style.display = "flex";
  const titleText = document.getElementById("rolling-modal-title-text");
  if (titleText) {
    titleText.textContent = action === 'restart' ? 'ワーカー ローリング再起動' : 'ワーカー ローリング更新';
  }
  pollRollingStatus();
  if (window._rollingPollInterval) clearInterval(window._rollingPollInterval);
  window._rollingPollInterval = setInterval(pollRollingStatus, 2500);
}

function closeRollingProgressModal(eventOrForce) {
  if (eventOrForce && eventOrForce.target && eventOrForce.target.id !== "rolling-progress-modal" && eventOrForce !== true) {
    return;
  }
  const modal = document.getElementById("rolling-progress-modal");
  if (!modal) return;
  modal.style.display = "none";
  if (window._rollingPollInterval) {
    clearInterval(window._rollingPollInterval);
    window._rollingPollInterval = null;
  }
}

async function pollRollingStatus() {
  try {
    const resp = await fetch('/api/worker/rolling/status');
    const data = await resp.json();
    const st = data.state || {};
    renderRollingStateUI(st);
  } catch (err) {
    console.warn("pollRollingStatus error:", err);
  }
}

function renderRollingStateUI(st) {
  const summaryEl = document.getElementById("rolling-status-summary");
  const ratioEl = document.getElementById("rolling-status-ratio");
  const barEl = document.getElementById("rolling-progress-bar");
  const detailEl = document.getElementById("rolling-current-detail");
  const listEl = document.getElementById("rolling-nodes-list");
  if (!summaryEl || !ratioEl || !barEl || !detailEl || !listEl) return;

  const total = st.total || 0;
  const done = st.done || 0;
  const running = !!st.running;
  const actionLabel = st.action === 'restart' ? '再起動' : '更新';

  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  barEl.style.width = `${pct}%`;
  ratioEl.textContent = `${done} / ${total} 台完了 (${pct}%)`;

  if (running) {
    const cur = st.current || st.current_ip || "準備中";
    summaryEl.innerHTML = `🔄 処理中: <strong style="color:#50e6ff;">${escapeHtml(cur)}</strong> (${actionLabel})`;
    detailEl.textContent = st.current_detail ? `▶ ${st.current_detail}` : "";
    barEl.style.background = "linear-gradient(90deg, #5865f2, #00b0f4)";
  } else {
    if (st.error) {
      summaryEl.innerHTML = `❌ <span style="color:#f04747;">エラー中断: ${escapeHtml(st.error)}</span>`;
      barEl.style.background = "#f04747";
    } else if (done > 0 && done === total) {
      summaryEl.innerHTML = `✅ <span style="color:#a8ff78;">全 ${total} 台の${actionLabel}が正常に完了しました</span>`;
      barEl.style.background = "#23a55a";
    } else {
      summaryEl.innerHTML = `待機中 / 完了`;
    }
    detailEl.textContent = "";
    if (window._rollingPollInterval) {
      clearInterval(window._rollingPollInterval);
      window._rollingPollInterval = null;
    }
    if (typeof fetchSystemStats === 'function') fetchSystemStats();
  }

  // ノードリスト描画
  const results = Array.isArray(st.results) ? st.results : [];
  let html = '';
  results.forEach(r => {
    const icon = r.skipped ? '⏭️' : r.success ? '✅' : '❌';
    const color = r.skipped ? 'var(--text-muted)' : r.success ? '#a8ff78' : '#f04747';
    html += `
      <div style="display:flex;justify-content:space-between;align-items:center;background:rgba(255,255,255,0.03);padding:6px 10px;border-radius:4px;font-size:11px;">
        <span style="font-weight:600;">${icon} ${escapeHtml(r.label || r.ip)}</span>
        <span style="color:${color};font-family:monospace;">${escapeHtml(r.message || '')}</span>
      </div>`;
  });
  if (running && st.current) {
    html += `
      <div style="display:flex;justify-content:space-between;align-items:center;background:rgba(88,101,242,0.15);border:1px solid rgba(88,101,242,0.3);padding:6px 10px;border-radius:4px;font-size:11px;">
        <span style="font-weight:600;color:#50e6ff;">⏳ ${escapeHtml(st.current)} (${escapeHtml(st.current_ip || '')})</span>
        <span style="color:#a8ff78;font-size:10px;">${escapeHtml(st.current_detail || '進行中...')}</span>
      </div>`;
  }
  listEl.innerHTML = html || '<div style="color:var(--text-dim);font-size:11px;text-align:center;padding:8px;">対象ノード待機中...</div>';
}

async function triggerWorkerUpdateAll() {
  return triggerRollingJob('update');
}


function copyToClipboard(elementId, btn) {
  const el = document.getElementById(elementId);
  if (!el) return;
  const text = el.textContent || el.innerText;
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.innerHTML;
    btn.innerHTML = "✅ コピー完了！";
    btn.style.background = "#23a55a";
    btn.style.color = "white";
    setTimeout(() => {
      btn.innerHTML = orig;
      btn.style.background = "";
      btn.style.color = "";
    }, 2000);
  }).catch(err => {
    console.error("Clipboard copy failed:", err);
  });
}

function renderTtsUsage(usage) {
  const monthBadge = document.getElementById("tts-month-badge");
  if (monthBadge && usage.month) {
    monthBadge.textContent = `${usage.month} 集計`;
  }
  const monthBadgeLocal = document.getElementById("tts-month-badge-local");
  if (monthBadgeLocal && usage.month) {
    monthBadgeLocal.textContent = `${usage.month} 集計`;
  }

  // Azure
  const azure = usage.azure || { characters: 0, limit: 500000, requests: 0 };
  const azureChars = azure.characters || 0;
  const azureLimit = azure.limit || 500000;
  const azurePct = Math.min(100, (azureChars / azureLimit) * 100);
  const azureRemaining = Math.max(0, azureLimit - azureChars);

  const azureText = document.getElementById("azure-usage-text");
  const azureFill = document.getElementById("azure-progress-fill");
  const azureReqs = document.getElementById("azure-requests-text");
  const azureRem = document.getElementById("azure-remaining-text");

  if (azureText) azureText.textContent = `${azureChars.toLocaleString()} / ${azureLimit.toLocaleString()} 文字 (${azurePct.toFixed(1)}%)`;
  if (azureFill) {
    azureFill.style.width = `${azurePct}%`;
    if (azurePct >= 90) {
      azureFill.style.background = "linear-gradient(90deg, #ff4757, #ff6b81)";
    } else if (azurePct >= 70) {
      azureFill.style.background = "linear-gradient(90deg, #ffa502, #eccc68)";
    } else {
      azureFill.style.background = "linear-gradient(90deg, #0078d4, #50e6ff)";
    }
  }
  if (azureReqs) azureReqs.textContent = `リクエスト: ${azure.requests || 0}回`;
  if (azureRem) azureRem.textContent = `残り無料枠: ${azureRemaining.toLocaleString()}文字`;

  // Google (無料枠の95% = 950,000文字を上限ガード)
  const google = usage.google || { characters: 0, limit: 1000000, requests: 0 };
  const googleChars = google.characters || 0;
  const googleSafeLimit = 950000; // 95%上限ガード
  const googlePct = Math.min(100, (googleChars / googleSafeLimit) * 100);
  const googleRemaining = Math.max(0, googleSafeLimit - googleChars);

  const googleText = document.getElementById("google-usage-text");
  const googleFill = document.getElementById("google-progress-fill");
  const googleReqs = document.getElementById("google-requests-text");
  const googleRem = document.getElementById("google-remaining-text");

  if (googleText) googleText.textContent = `${googleChars.toLocaleString()} / ${googleSafeLimit.toLocaleString()} 文字 (${googlePct.toFixed(1)}%)`;
  if (googleFill) {
    googleFill.style.width = `${googlePct}%`;
    if (googlePct >= 95) {
      googleFill.style.background = "linear-gradient(90deg, #ff4757, #ff6b81)";
    } else if (googlePct >= 75) {
      googleFill.style.background = "linear-gradient(90deg, #ffa502, #eccc68)";
    } else {
      googleFill.style.background = "linear-gradient(90deg, #ea4335, #fbbc05)";
    }
  }
  if (googleReqs) googleReqs.textContent = `リクエスト: ${google.requests || 0}回`;
  if (googleRem) googleRem.textContent = `残り安全枠: ${googleRemaining.toLocaleString()}文字 (95%到達でVOICEVOXへ自動切替)`;

  // Edge-TTS (完全無料クラウド・無制限)
  const edge = usage.edge || { characters: 0, requests: 0 };
  const edgeChars = edge.characters || 0;
  const edgeReqs = edge.requests || 0;
  const edgeText = document.getElementById("edge-usage-text");
  const edgeReqsEl = document.getElementById("edge-requests-text");
  if (edgeText) edgeText.textContent = `${edgeChars.toLocaleString()} 文字合成 (${edgeReqs.toLocaleString()}回)`;
  if (edgeReqsEl) edgeReqsEl.textContent = `リクエスト: ${edgeReqs.toLocaleString()}回 (完全無料・APIキー不要)`;

  // VOICEVOX (完全ローカル・無制限)
  const voicevox = usage.voicevox || { characters: 0, requests: 0 };
  const vvChars = voicevox.characters || 0;
  const vvReqs = voicevox.requests || 0;
  const vvText = document.getElementById("voicevox-usage-text");
  const vvReqsEl = document.getElementById("voicevox-requests-text");
  if (vvText) vvText.textContent = `${vvChars.toLocaleString()} 文字合成 (${vvReqs.toLocaleString()}回)`;
  if (vvReqsEl) vvReqsEl.textContent = `リクエスト: ${vvReqs.toLocaleString()}回 (完全無料・無制限)`;

  // SHAREVOX (完全ローカル)
  const sharevox = usage.sharevox || { characters: 0, requests: 0 };
  const svChars = sharevox.characters || 0;
  const svReqs = sharevox.requests || 0;
  const svText = document.getElementById("sharevox-usage-text");
  const svReqsEl = document.getElementById("sharevox-requests-text");
  if (svText) svText.textContent = `${svChars.toLocaleString()} 文字合成 (${svReqs.toLocaleString()}回)`;
  if (svReqsEl) svReqsEl.textContent = `リクエスト: ${svReqs.toLocaleString()}回 (直列化ロックでCPU安定稼働)`;

  // COEIROINK (完全ローカル)
  const coeiroink = usage.coeiroink || { characters: 0, requests: 0 };
  const ciChars = coeiroink.characters || 0;
  const ciReqs = coeiroink.requests || 0;
  const ciText = document.getElementById("coeiroink-usage-text");
  const ciReqsEl = document.getElementById("coeiroink-requests-text");
  if (ciText) ciText.textContent = `${ciChars.toLocaleString()} 文字合成 (${ciReqs.toLocaleString()}回)`;
  if (ciReqsEl) ciReqsEl.textContent = `リクエスト: ${ciReqs.toLocaleString()}回 (高品質AI音声)`;

  // ⚡ スマートパーツキャッシュ節約 (クラウド / ローカル完全分離)
  const savings = usage.savings || {
    saved_characters: 0,
    hits: 0,
    hit_rate_percent: 0.0,
    cloud: { saved_characters: 0, hits: 0, hit_rate_percent: 0.0 },
    local: { saved_characters: 0, hits: 0, hit_rate_percent: 0.0 }
  };

  // 1. ☁️ クラウドTTS スマートパーツキャッシュ節約
  const cloud = savings.cloud || { saved_characters: 0, hits: 0, hit_rate_percent: 0.0 };
  const cloudSaved = cloud.saved_characters || 0;
  const cloudHits = cloud.hits || 0;
  const cloudRate = cloud.hit_rate_percent || 0.0;

  const cloudBadge = document.getElementById("savings-cloud-badge");
  const cloudFill = document.getElementById("savings-cloud-fill");
  const cloudHitsEl = document.getElementById("savings-cloud-hits");
  const cloudSavedEl = document.getElementById("savings-cloud-saved");

  if (cloudBadge) cloudBadge.textContent = `当月節約 ${cloudSaved.toLocaleString()} 文字 (ヒット率 ${cloudRate.toFixed(1)}%)`;
  if (cloudFill) cloudFill.style.width = `${Math.min(100, cloudRate)}%`;
  if (cloudHitsEl) cloudHitsEl.textContent = `当月ヒット: ${cloudHits.toLocaleString()}回`;
  if (cloudSavedEl) cloudSavedEl.textContent = `当月節約: ${cloudSaved.toLocaleString()}文字`;

  // 2. 💻 ローカルTTS スマートパーツキャッシュ節約
  const local = savings.local || { saved_characters: 0, hits: 0, hit_rate_percent: 0.0 };
  const localSaved = local.saved_characters || 0;
  const localHits = local.hits || 0;
  const localRate = local.hit_rate_percent || 0.0;

  const localBadge = document.getElementById("savings-local-badge");
  const localFill = document.getElementById("savings-local-fill");
  const localHitsEl = document.getElementById("savings-local-hits");
  const localSavedEl = document.getElementById("savings-local-saved");

  if (localBadge) localBadge.textContent = `当月節約 ${localSaved.toLocaleString()} 文字 (ヒット率 ${localRate.toFixed(1)}%)`;
  if (localFill) localFill.style.width = `${Math.min(100, localRate)}%`;
  if (localHitsEl) localHitsEl.textContent = `当月ヒット: ${localHits.toLocaleString()}回`;
  if (localSavedEl) localSavedEl.textContent = `当月節約: ${localSaved.toLocaleString()}文字`;
}

// --- 📈 システムリソース & 各エンジン負荷推移 (30秒間隔・適応型並列制御) ---

let systemMetricsTimer = null;
let latestMetricsData = null;

const METRICS_SERIES = [
  { key: "host", label: "ホスト全体", color: "#95a5a6", dashed: true, width: 1.5 },
  { key: "bot", label: "Bot本体", color: "#5865f2", dashed: false, width: 2 },
  { key: "voicevox", label: "VOICEVOX", color: "#2ecc71", dashed: false, width: 2 },
  { key: "sharevox", label: "SHAREVOX", color: "#3498db", dashed: false, width: 2 },
  { key: "coeiroink", label: "COEIROINK", color: "#9b59b6", dashed: false, width: 2 },
];

const WORKER_CPU_SERIES = [
  { key: "cpu_percent", label: "CPU使用率", color: "#5865f2", dashed: false, width: 2 },
  { key: "gpu_util_percent", label: "GPU使用率", color: "#2ecc71", dashed: false, width: 2 },
];

const WORKER_MEM_SERIES = [
  { key: "mem_used_gb", label: "RAM使用量", color: "#3498db", dashed: false, width: 2 },
  { key: "gpu_mem_used_gb", label: "VRAM使用量", color: "#9b59b6", dashed: false, width: 2 },
];

function updateMetricsLegend(nodeKey) {
  const legendEl = document.getElementById("metrics-legend");
  if (!legendEl) return;
  if (!nodeKey || nodeKey === 'master' || nodeKey === 'masterdebian') {
    legendEl.innerHTML = `
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#95a5a6;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">ホスト全体</span>
      </div>
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#5865f2;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">Bot本体</span>
      </div>
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#2ecc71;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">VOICEVOX</span>
      </div>
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#3498db;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">SHAREVOX</span>
      </div>
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#9b59b6;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">COEIROINK</span>
      </div>
    `;
  } else if (nodeKey === 'masterubuntu') {
    legendEl.innerHTML = `
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#5865f2;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">CPU使用率 (%)</span>
      </div>
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#3498db;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">RAM使用量 (GB)</span>
      </div>
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#747f8d;border-radius:2px;"></span>
        <span style="color:var(--text-muted);">GPU非搭載 (CPU専用)</span>
      </div>
    `;
  } else {
    legendEl.innerHTML = `
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#5865f2;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">CPU使用率 (%)</span>
      </div>
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#2ecc71;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">GPU使用率 (%)</span>
      </div>
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#3498db;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">RAM使用量 (GB)</span>
      </div>
      <div style="display:flex;align-items:center;gap:5px;">
        <span style="display:inline-block;width:12px;height:12px;background:#9b59b6;border-radius:2px;"></span>
        <span style="color:var(--text-normal);">VRAM使用量 (GB)</span>
      </div>
    `;
  }
}

function startSystemMetricsPolling() {
  if (systemMetricsTimer) return;
  fetchSystemMetrics();
  systemMetricsTimer = setInterval(fetchSystemMetrics, 30000);
}

function stopSystemMetricsPolling() {
  if (systemMetricsTimer) {
    clearInterval(systemMetricsTimer);
    systemMetricsTimer = null;
  }
}

async function fetchSystemMetrics() {
  if (!currentGuildId) return;
  try {
    const res = await fetch(`/api/system/metrics?guild_id=${encodeURIComponent(currentGuildId)}`, { cache: "no-store" });
    if (res.status === 403) {
      stopSystemMetricsPolling();
      return;
    }
    if (!res.ok) return;
    const data = await res.json();
    latestMetricsData = data;
    renderSystemMetricsUI(data);
    // メトリクス更新周期 (30秒) に合わせてワーカーノード一覧およびシステム統計も最新に維持
    loadStats(currentGuildId);
  } catch (e) {
    console.error("Failed to fetch system metrics:", e);
  }
}

function renderSystemMetricsUI(data) {
  if (!data) return;
  const nodeKey = window.currentMetricsNode || 'master';
  updateMetricsLegend(nodeKey);

  const status = data.status || {};
  const history = data.history || [];

  // 1. 稼働モードバッジ
  const badgeMode = document.getElementById("metrics-concurrency-badge");
  if (badgeMode) {
    if (status.mode === "parallel") {
      badgeMode.textContent = "⚡ 並列合成モード (最大2並列)";
      badgeMode.style.background = "#3ba55c";
    } else if (status.mode === "serial") {
      badgeMode.textContent = "⚠️ 直列合成モード (CPU高負荷 1並列)";
      badgeMode.style.background = "#faa61a";
    } else if (status.mode === "cloud_fallback") {
      badgeMode.textContent = "🛡️ Edge-TTS退避モード (CPU危険域)";
      badgeMode.style.background = "#ed4245";
    } else {
      badgeMode.textContent = "⚡ 並列合成モード";
      badgeMode.style.background = "#3ba55c";
    }
  }

  // 2. 合成スロット稼働バッジ
  const badgeSlots = document.getElementById("metrics-active-slots-badge");
  if (badgeSlots) {
    const maxSlots = status.mode === "serial" ? 1 : 2;
    badgeSlots.textContent = `合成中: ${status.active_synthesis || 0} / ${maxSlots}`;
  }

  // 3. 最終更新時刻
  const lastUpdatedEl = document.getElementById("metrics-last-updated");
  if (lastUpdatedEl) {
    const now = new Date();
    lastUpdatedEl.textContent = `更新: ${now.toLocaleTimeString()}`;
  }

  // 4. ノード別現在値および折れ線グラフ描画
  const hostCpuEl = document.getElementById("metrics-current-cpu");
  const hostMemEl = document.getElementById("metrics-current-mem");

  if (nodeKey === 'master') {
    // --- Master (自機) 表示 ---
    if (hostCpuEl) {
      const cpuVal = typeof status.host_cpu === "number" ? status.host_cpu.toFixed(1) : "--";
      hostCpuEl.textContent = `ホスト: ${cpuVal}%`;
      if (status.host_cpu >= 85) hostCpuEl.style.color = "#ed4245";
      else if (status.host_cpu >= 70) hostCpuEl.style.color = "#faa61a";
      else hostCpuEl.style.color = "#3ba55c";
    }

    if (hostMemEl && history.length > 0) {
      const lastPoint = history[history.length - 1];
      if (lastPoint && lastPoint.host) {
        hostMemEl.textContent = `ホスト: ${lastPoint.host.memory_used_gb || "--"} GB (${lastPoint.host.memory_percent || "--"}%)`;
      }
    }

    renderCpuChart(history);
    renderMemoryChart(history);
  } else {
    // --- Worker ノード表示 ---
    const wHistory = (data.workers_history && data.workers_history[nodeKey]) || [];
    const latestPt = wHistory.length > 0 ? wHistory[wHistory.length - 1] : (data.workers_current && data.workers_current[nodeKey]);

    if (hostCpuEl) {
      const cpuVal = (latestPt && typeof latestPt.cpu_percent === 'number') ? latestPt.cpu_percent.toFixed(1) : '--';
      const gpuVal = (latestPt && typeof latestPt.gpu_util_percent === 'number') ? `${latestPt.gpu_util_percent.toFixed(1)}%` : (nodeKey === 'masterubuntu' ? 'なし (CPU専用)' : 'N/A');
      hostCpuEl.textContent = `CPU: ${cpuVal}% | GPU: ${gpuVal}`;
      if (latestPt && typeof latestPt.cpu_percent === 'number') {
        if (latestPt.cpu_percent >= 85) hostCpuEl.style.color = "#ed4245";
        else if (latestPt.cpu_percent >= 70) hostCpuEl.style.color = "#faa61a";
        else hostCpuEl.style.color = "#3ba55c";
      } else {
        hostCpuEl.style.color = "var(--text-muted)";
      }
    }

    if (hostMemEl) {
      const ramVal = (latestPt && typeof latestPt.mem_used_gb === 'number') ? `${latestPt.mem_used_gb} GB` : '-- GB';
      const vramVal = (latestPt && typeof latestPt.gpu_mem_used_gb === 'number' && latestPt.gpu_mem_used_gb > 0) ? `${latestPt.gpu_mem_used_gb} GB` : (nodeKey === 'masterubuntu' ? 'なし' : 'N/A');
      hostMemEl.textContent = `RAM: ${ramVal} | VRAM: ${vramVal}`;
    }

    renderWorkerCpuChart(wHistory);
    renderWorkerMemoryChart(wHistory);
  }

  // 6. 発話〜再生レイテンシ モニター描画
  if (data.latency) {
    renderLatencyMonitorUI(data.latency, data.profiler);
  }
}

function formatSmartWorker(rawLabel, provider) {
  if (!rawLabel) {
    const eng = (provider || "VOICEVOX").toUpperCase();
    return { machine: "MasterDebian", engine: eng };
  }
  let str = String(rawLabel).trim();
  if (str.includes(" / ")) {
    const parts = str.split(" / ");
    return { machine: parts[0].trim(), engine: parts[1].trim() };
  }
  let machine = str || "Primary Node";
  if (str.includes("127.0.0.1") || str.toLowerCase().includes("master") || str.toLowerCase().includes("local")) {
    machine = "Primary Node";
  }

  let engine = (provider || "").toUpperCase();
  if (str.includes(":5002") || str.toLowerCase().includes("voicevox")) engine = "VOICEVOX";
  else if (str.includes(":5003") || str.toLowerCase().includes("coeiroink")) engine = "COEIROINK";
  else if (str.includes(":10101") || str.toLowerCase().includes("aivis")) engine = "AIVISSPEECH";
  else if (str.includes(":8000") || str.toLowerCase().includes("sharevox")) engine = "SHAREVOX";
  else if (!engine) engine = "VOICEVOX";

  return { machine, engine };
}

function renderLatencyMonitorUI(latencyData, profilerData) {
  if (!latencyData) return;
  const summary = latencyData.summary || {};
  const history = latencyData.history || [];
  const workerStats = latencyData.workers || latencyData.worker_stats || {};
  const profiler = profilerData || {};

  // フォールバック集計
  if ((!summary.total_count || summary.total_count === 0) && history.length > 0) {
    const totals = history.map(h => h.total_latency_ms || 0);
    const synths = history.map(h => h.synthesis_ms || 0);
    const queues = history.map(h => h.queue_wait_ms || 0);
    const discords = history.map(h => h.discord_latency_ms || 0);
    const preps = history.map(h => h.play_prep_ms || 0);
    const turnTtfses = history.map(h => (h.turn_ttfs_ms !== undefined ? h.turn_ttfs_ms : (h.ttfs_ms || h.total_latency_ms)));
    const cachedCnt = history.filter(h => h.is_cached).length;
    summary.total_count = history.length;
    summary.sample_count = history.length;
    summary.cache_hits = cachedCnt;
    summary.cache_hit_rate_percent = Math.round((cachedCnt / history.length) * 1000) / 10;
    summary.avg_total_latency_ms = Math.round(totals.reduce((a, b) => a + b, 0) / totals.length);
    summary.avg_synthesis_ms = Math.round(synths.reduce((a, b) => a + b, 0) / synths.length);
    summary.avg_queue_wait_ms = Math.round(queues.reduce((a, b) => a + b, 0) / queues.length);
    summary.avg_discord_latency_ms = Math.round(discords.reduce((a, b) => a + b, 0) / discords.length);
    summary.avg_play_prep_ms = Math.round(preps.reduce((a, b) => a + b, 0) / preps.length);
    summary.avg_turn_ttfs_ms = Math.round(turnTtfses.reduce((a, b) => a + b, 0) / turnTtfses.length);
    summary.min_total_latency_ms = Math.min(...totals);
    summary.max_total_latency_ms = Math.max(...totals);
  }

  // 1. バッジ更新
  const overallBadge = document.getElementById("latency-overall-badge");
  if (overallBadge) {
    if (!summary.total_count || summary.total_count === 0) {
      overallBadge.textContent = "⚡ 計測待機中";
      overallBadge.style.background = "#747f8d";
    } else {
      const turnAvg = (summary.avg_turn_ttfs_ms !== undefined) ? summary.avg_turn_ttfs_ms : (summary.avg_ttfs_ms || summary.avg_total_latency_ms);
      if (turnAvg < 250) {
        overallBadge.textContent = `⚡ 神速即応 (${turnAvg}ms)`;
        overallBadge.style.background = "#2ecc71";
      } else if (turnAvg < 600) {
        overallBadge.textContent = `⚡ 高速即応 (${turnAvg}ms)`;
        overallBadge.style.background = "#5865f2";
      } else if (turnAvg < 1500) {
        overallBadge.textContent = `⚠️ 通常 (${turnAvg}ms)`;
        overallBadge.style.background = "#faa61a";
      } else {
        overallBadge.textContent = `🐢 待機あり (${turnAvg}ms)`;
        overallBadge.style.background = "#ed4245";
      }
    }
  }

  const lastUpdatedEl = document.getElementById("latency-last-updated");
  if (lastUpdatedEl) {
    const now = new Date();
    lastUpdatedEl.textContent = `更新: ${now.toLocaleTimeString()}`;
  }

  // 2. KPI メーター
  const kpiTtfs = document.getElementById("lat-kpi-ttfs");
  const kpiTtfsCard = document.getElementById("lat-kpi-ttfs-card");
  const kpiTtfsBar = document.getElementById("lat-kpi-ttfs-bar");
  const kpiTtfsDetail = document.getElementById("lat-kpi-ttfs-detail");
  if (kpiTtfs) {
    const ttfsVal = (summary.avg_turn_ttfs_ms !== undefined) ? summary.avg_turn_ttfs_ms : (summary.avg_ttfs_ms || summary.avg_total_latency_ms);
    const coreVal = summary.avg_core_ttfs_ms || 0;
    const prerollVal = summary.avg_preroll_wait_ms || 0;

    kpiTtfs.textContent = (summary.total_count > 0) ? `${ttfsVal} ms` : "-- ms";

    let ttfsColor = "#50e6ff";
    if (ttfsVal < 250) ttfsColor = "#50e6ff";
    else if (ttfsVal < 600) ttfsColor = "#a8ff78";
    else if (ttfsVal < 1500) ttfsColor = "#f39c12";
    else ttfsColor = "#ed4245";

    kpiTtfs.style.color = ttfsColor;
    if (kpiTtfsBar) kpiTtfsBar.style.background = ttfsColor;
    if (kpiTtfsCard) kpiTtfsCard.style.borderColor = `${ttfsColor}40`;

    if (kpiTtfsDetail) {
      if (summary.total_count > 0 && (coreVal > 0 || prerollVal > 0)) {
        kpiTtfsDetail.innerHTML = `
          <span style="background:rgba(80,230,255,0.15);color:#50e6ff;padding:1px 5px;border-radius:3px;font-weight:600;" title="推論起因ラグ (第1チャンク合成完了)">Core: ${coreVal}ms</span>
          <span style="color:var(--text-dim);font-weight:bold;">+</span>
          <span style="background:rgba(230,126,34,0.15);color:#e67e22;padding:1px 5px;border-radius:3px;font-weight:600;" title="Adaptive Preroll待機時間 (第2チャンク合成待ち)">Preroll: ${prerollVal}ms</span>
        `;
      } else {
        kpiTtfsDetail.textContent = 'キュー待ち除外・目標 <250ms';
      }
    }
  }

  const kpiSeamless = document.getElementById("lat-kpi-seamless");
  if (kpiSeamless) {
    const sRate = (summary.underrun_free_rate_percent !== undefined) ? summary.underrun_free_rate_percent : ((summary.seamless_rate_percent !== undefined) ? summary.seamless_rate_percent : 100.0);
    kpiSeamless.textContent = (summary.total_count > 0) ? `${sRate} %` : "100 %";
    kpiSeamless.style.color = (sRate >= 98) ? "#2ecc71" : ((sRate >= 90) ? "#f1c40f" : "#ed4245");
  }

  const kpiRtf = document.getElementById("lat-kpi-rtf");
  if (kpiRtf) {
    const rtfVal = summary.avg_rtf || 0;
    if (summary.total_count > 0 && rtfVal > 0) {
      const speed = (1 / rtfVal).toFixed(1);
      kpiRtf.textContent = `${rtfVal}x (${speed}倍速)`;
      kpiRtf.style.color = (rtfVal < 0.3) ? "#50e6ff" : (rtfVal < 1.0 ? "#a8ff78" : "#f39c12");
    } else {
      kpiRtf.textContent = "-- x";
      kpiRtf.style.color = "#a8ff78";
    }
  }

  // 🛡️ 最小バッファ余裕 (Headroom) インテリジェント4段階判定
  const kpiHeadroom = document.getElementById("lat-kpi-headroom");
  const kpiHeadroomCard = document.getElementById("lat-kpi-headroom-card");
  const kpiHeadroomBar = document.getElementById("lat-kpi-headroom-bar");
  const kpiHeadroomDetail = document.getElementById("lat-kpi-headroom-detail");
  if (kpiHeadroom) {
    const hrVal = summary.avg_buffer_headroom_ms || 0;
    kpiHeadroom.textContent = (summary.total_count > 0) ? `${hrVal >= 0 ? '+' : ''}${hrVal} ms` : "+-- ms";

    let hrColor = "#2ecc71";
    let hrDetailHtml = "再生期日への先回りリード";
    if (summary.total_count > 0) {
      if (hrVal < 0) {
        hrColor = "#ed4245"; // 負値: アンダーラン/音途切れ発生
        hrDetailHtml = `<span style="color:#ed4245;font-weight:600;">⚠️ 枯渇発生 (音途切れリスク)</span>`;
      } else if (hrVal < 200) {
        hrColor = "#f1c40f"; // 僅少: ギリギリ
        hrDetailHtml = `<span style="color:#f1c40f;font-weight:600;">⚡ 余裕僅少 (&lt;200ms)</span>`;
      } else if (hrVal < 800) {
        hrColor = "#a8ff78"; // 安全圏: スムーズ
        hrDetailHtml = `<span style="color:#a8ff78;font-weight:600;">🟢 安全圏 (シームレス先行完了)</span>`;
      } else {
        hrColor = "#50e6ff"; // 潤沢: 完全シームレス
        hrDetailHtml = `<span style="color:#50e6ff;font-weight:600;">🛡️ 潤沢リード (+800ms以上)</span>`;
      }
    }
    kpiHeadroom.style.color = hrColor;
    if (kpiHeadroomBar) kpiHeadroomBar.style.background = hrColor;
    if (kpiHeadroomCard) kpiHeadroomCard.style.borderColor = `${hrColor}40`;
    if (kpiHeadroomDetail) kpiHeadroomDetail.innerHTML = hrDetailHtml;
  }

  const kpiCount = document.getElementById("lat-kpi-count");
  if (kpiCount) {
    kpiCount.textContent = `${summary.total_count || 0} 件`;
  }
  const kpiMinMax = document.getElementById("lat-kpi-minmax");
  if (kpiMinMax) {
    if (summary.total_count > 0) {
      kpiMinMax.textContent = `最速 ${summary.min_total_latency_ms}ms / 最遅 ${summary.max_total_latency_ms}ms`;
    } else {
      kpiMinMax.textContent = "最速 --ms / 最遅 --ms";
    }
  }

  // 3. 遅延内訳 (プログレスバー) - Discord → Queue → Synth → Preroll → Prep
  const discordMs = summary.avg_discord_latency_ms || 0;
  const queueMs = summary.avg_queue_wait_ms || 0;
  const synthMs = summary.avg_synthesis_ms || 0;
  const prerollMs = summary.avg_preroll_wait_ms || 0;
  const prepMs = summary.avg_play_prep_ms || 0;
  const sumMs = discordMs + queueMs + synthMs + prerollMs + prepMs;

  const totalLbl = document.getElementById("lat-breakdown-total");
  if (totalLbl) {
    totalLbl.textContent = (summary.total_count > 0) ? `平均所要: ${sumMs.toFixed(1)} ms` : "合計: -- ms";
  }

  const barDiscord = document.getElementById("lat-bar-discord");
  const barQueue = document.getElementById("lat-bar-queue");
  const barSynth = document.getElementById("lat-bar-synth");
  const barPreroll = document.getElementById("lat-bar-preroll");
  const barPrep = document.getElementById("lat-bar-prep");

  if (sumMs > 0) {
    const dPct = ((discordMs / sumMs) * 100).toFixed(1);
    const qPct = ((queueMs / sumMs) * 100).toFixed(1);
    const sPct = ((synthMs / sumMs) * 100).toFixed(1);
    const prePct = ((prerollMs / sumMs) * 100).toFixed(1);
    const pPct = ((prepMs / sumMs) * 100).toFixed(1);

    if (barDiscord) {
      barDiscord.style.width = `${(discordMs / sumMs) * 100}%`;
      barDiscord.title = `Discord通信: ${discordMs.toFixed(1)}ms (${dPct}%)`;
    }
    if (barQueue) {
      barQueue.style.width = `${(queueMs / sumMs) * 100}%`;
      barQueue.title = `キュー待機: ${queueMs.toFixed(1)}ms (${qPct}%)`;
    }
    if (barSynth) {
      barSynth.style.width = `${(synthMs / sumMs) * 100}%`;
      barSynth.title = `TTS推論 (Core): ${synthMs.toFixed(1)}ms (${sPct}%)`;
    }
    if (barPreroll) {
      barPreroll.style.width = `${(prerollMs / sumMs) * 100}%`;
      barPreroll.title = `Preroll待機 (バッファ蓄積): ${prerollMs.toFixed(1)}ms (${prePct}%)`;
    }
    if (barPrep) {
      barPrep.style.width = `${(prepMs / sumMs) * 100}%`;
      barPrep.title = `再生準備: ${prepMs.toFixed(1)}ms (${pPct}%)`;
    }
  } else {
    if (barDiscord) barDiscord.style.width = "0%";
    if (barQueue) barQueue.style.width = "0%";
    if (barSynth) barSynth.style.width = "0%";
    if (barPreroll) barPreroll.style.width = "0%";
    if (barPrep) barPrep.style.width = "0%";
  }

  const valDiscord = document.getElementById("lat-val-discord");
  if (valDiscord) valDiscord.textContent = (summary.total_count > 0) ? `${discordMs.toFixed(1)} ms` : "-- ms";
  const valQueue = document.getElementById("lat-val-queue");
  if (valQueue) valQueue.textContent = (summary.total_count > 0) ? `${queueMs.toFixed(1)} ms` : "-- ms";
  const valSynth = document.getElementById("lat-val-synth");
  if (valSynth) valSynth.textContent = (summary.total_count > 0) ? `${synthMs.toFixed(1)} ms` : "-- ms";
  const valPreroll = document.getElementById("lat-val-preroll");
  if (valPreroll) {
    valPreroll.textContent = (summary.total_count > 0) ? `${prerollMs.toFixed(1)} ms` : "-- ms";
    valPreroll.style.color = (prerollMs > 0) ? "#e67e22" : "#eee";
  }
  const valPrep = document.getElementById("lat-val-prep");
  if (valPrep) valPrep.textContent = (summary.total_count > 0) ? `${prepMs.toFixed(1)} ms` : "-- ms";

  // 4. ワーカー別 推論速度サマリー
  const workerContainer = document.getElementById("latency-worker-summary-cards");
  if (workerContainer) {
    const workerEntries = Object.entries(workerStats);
    if (workerEntries.length === 0) {
      workerContainer.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:8px;">計測データ待機中...</div>';
    } else {
      const profNodes = (profiler && profiler.nodes) ? profiler.nodes : {};
      const strongestNodes = (profiler && profiler.strongest_nodes) ? profiler.strongest_nodes : {};
      const strongestMachines = (profiler && profiler.strongest_machines) ? profiler.strongest_machines : {};

      workerContainer.innerHTML = workerEntries.map(([lbl, stat]) => {
        const sw = formatSmartWorker(lbl, "");
        const isGpu = sw.machine.includes("2022") || sw.machine.includes("2017") || lbl.includes("GPU") || lbl.includes("3090") || lbl.includes("1080");
        const isCache = lbl.includes("キャッシュ");
        const icon = isCache ? "⚡" : (isGpu ? "🎮" : "🏢");
        const color = isCache ? "#a8ff78" : (isGpu ? "#50e6ff" : "#f39c12");

        // プロファイラからノード統計を検索
        let nodeProf = null;
        for (const [key, p] of Object.entries(profNodes)) {
          if (
            (p.machine_name && sw.machine.includes(p.machine_name)) ||
            (p.machine_name && p.machine_name.includes(sw.machine)) ||
            key.includes(sw.machine) ||
            lbl.includes(p.node_id) ||
            (p.ip && lbl.includes(p.ip))
          ) {
            nodeProf = p;
            break;
          }
        }

        const engKey = sw.engine.toLowerCase();
        let isStrongest = false;
        if (nodeProf && nodeProf.is_strongest_gpu) {
          isStrongest = true;
        } else if (strongestMachines[engKey] && (sw.machine.includes(strongestMachines[engKey]) || strongestMachines[engKey].includes(sw.machine))) {
          isStrongest = true;
        } else if (strongestNodes[engKey] && (lbl.includes(strongestNodes[engKey]) || (nodeProf && nodeProf.node_id === strongestNodes[engKey]))) {
          isStrongest = true;
        } else if (isGpu && (sw.machine.includes("3090") || sw.machine.includes("2022"))) {
          // 最低限のフォールバック (RTX 3090 / OMEN2022 を最強GPUと認識)
          isStrongest = true;
        }

        // バッジ生成
        let tierBadge = '';
        if (isCache) {
          tierBadge = '<span class="badge" style="background:rgba(168,255,120,0.15);color:#a8ff78;font-size:9px;padding:1px 5px;border-radius:4px;">L1/L2 即答</span>';
        } else if (isStrongest) {
          tierBadge = '<span class="badge" style="background:rgba(255,215,0,0.2);color:#ffd700;font-size:9px;padding:1px 5px;border-radius:4px;border:1px solid rgba(255,215,0,0.4);">👑 最強GPU (長文担当)</span>';
        } else if (isGpu) {
          tierBadge = '<span class="badge" style="background:rgba(80,230,255,0.15);color:#50e6ff;font-size:9px;padding:1px 5px;border-radius:4px;">🎮 サブGPU (短文高速)</span>';
        } else {
          tierBadge = '<span class="badge" style="background:rgba(243,156,18,0.15);color:#f39c12;font-size:9px;padding:1px 5px;border-radius:4px;">🏢 CPUマシン</span>';
        }

        // ms/文字レート表示
        let speedRateHtml = '';
        if (nodeProf && nodeProf.sample_count > 0 && nodeProf.ms_per_char > 0) {
          speedRateHtml = `<div style="font-size:10px;color:#a8ff78;font-weight:600;margin-top:2px;">⚡ 約 ${nodeProf.ms_per_char.toFixed(1)} ms/字</div>`;
        } else {
          speedRateHtml = `<div style="font-size:9px;color:var(--text-dim);">推論速度</div>`;
        }

        return `
          <div style="background:rgba(0,0,0,0.25);padding:10px 12px;border-radius:6px;border:1px solid rgba(255,255,255,0.06);display:flex;align-items:center;gap:10px;${isStrongest ? 'border-left:3px solid #ffd700;' : ''}">
            <div style="font-size:20px;">${icon}</div>
            <div style="flex:1;min-width:0;">
              <div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:2px;">
                <span style="font-weight:600;font-size:12px;color:var(--text-normal);">${escapeHtml(sw.machine)}</span>
                <span style="font-size:10px;color:var(--text-dim);">/ ${escapeHtml(sw.engine)}</span>
                ${tierBadge}
              </div>
              <div style="font-size:10px;color:var(--text-dim);">
                ${stat.count}件処理 ${nodeProf && nodeProf.base_latency_ms ? `(基本遅延: ${Math.round(nodeProf.base_latency_ms)}ms)` : ''}
              </div>
            </div>
            <div style="text-align:right;">
              <div style="font-weight:700;font-size:14px;color:${color};">
                ${stat.avg_synthesis_ms} ms
              </div>
              ${speedRateHtml}
            </div>
          </div>
        `;
      }).join("");
    }
  }

  // 5. 直近履歴テーブル (アコーディオンガントチャート展開対応)
  const tbody = document.getElementById("latency-history-tbody");
  if (tbody) {
    if (history.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" style="padding:14px;text-align:center;color:var(--text-dim);">まだ発話再生履歴がありません。VCで発言するとここに実測ラグが表示されます。</td></tr>`;
    } else {
      tbody.innerHTML = history.slice(0, 15).map((item, idx) => {
        const turnTtfs = item.turn_ttfs_ms !== undefined ? item.turn_ttfs_ms : (item.ttfs_ms !== undefined ? item.ttfs_ms : item.total_latency_ms);
        const queueWait = item.queue_wait_ms || 0;
        const underrunMs = item.underrun_ms || 0;

        // 状態バッジ判定
        let statusBadge = '';
        if (underrunMs > 0) {
          statusBadge = `<span class="badge" style="background:#ed4245;color:white;font-size:9px;padding:2px 6px;font-weight:600;">🔴 音飛び (${underrunMs}ms)</span>`;
        } else if (item.is_cached) {
          statusBadge = '<span class="badge" style="background:#23a55a;color:white;font-size:9px;padding:2px 6px;font-weight:600;">⚡ キャッシュ</span>';
        } else if (item.is_fallback) {
          statusBadge = '<span class="badge" style="background:#f39c12;color:white;font-size:9px;padding:2px 6px;font-weight:600;">🛡️ 退避</span>';
        } else if (turnTtfs < 250) {
          statusBadge = '<span class="badge" style="background:#00b0f4;color:white;font-size:9px;padding:2px 6px;font-weight:600;">⚡ 神速即応</span>';
        } else if (turnTtfs < 600) {
          statusBadge = '<span class="badge" style="background:#5865f2;color:white;font-size:9px;padding:2px 6px;font-weight:600;">🟢 シームレス</span>';
        } else {
          statusBadge = '<span class="badge" style="background:#747f8d;color:white;font-size:9px;padding:2px 6px;">通常</span>';
        }

        // 初動ラグ (TTFS) セル - Core/Preroll分解表示
        const ttfsColor = turnTtfs < 250 ? "#50e6ff" : (turnTtfs < 600 ? "#a8ff78" : (turnTtfs < 1500 ? "#f39c12" : "#ed4245"));
        const queueWaitHtml = queueWait > 50 ? `<div style="font-size:9px;color:var(--text-dim);font-weight:normal;">(+${Math.round(queueWait)}ms待機)</div>` : '';
        const coreTtfs = item.core_ttfs_ms !== undefined ? item.core_ttfs_ms : (item.turn_ttfs_ms !== undefined ? Math.max(0, item.turn_ttfs_ms - (item.preroll_wait_ms || 0)) : 0);
        const prerollWait = item.preroll_wait_ms !== undefined ? item.preroll_wait_ms : 0;
        const maskingRatio = item.masking_ratio !== undefined ? item.masking_ratio : 0;
        let ttfsBreakdown = '';
        if (coreTtfs > 0 || prerollWait > 0) {
          ttfsBreakdown = `
            <div style="font-size:9px;font-weight:normal;display:flex;gap:3px;justify-content:flex-end;align-items:center;margin-top:2px;">
              <span style="background:rgba(80,230,255,0.15);color:#50e6ff;padding:0 3px;border-radius:2px;" title="推論起因ラグ (Core)">${Math.round(coreTtfs)}</span>
              <span style="color:var(--text-dim);font-size:8px;">+</span>
              <span style="background:rgba(230,126,34,0.15);color:#e67e22;padding:0 3px;border-radius:2px;" title="Preroll待機時間">${Math.round(prerollWait)}</span>
            </div>
          `;
        }
        let maskingHtml = '';
        if (maskingRatio > 0 && !item.is_cached && item.chunks && item.chunks.length > 1) {
          const mColor = maskingRatio >= 100 ? '#2ecc71' : (maskingRatio >= 80 ? '#a8ff78' : (maskingRatio >= 50 ? '#f1c40f' : '#ed4245'));
          maskingHtml = `<div style="font-size:9px;font-weight:600;color:${mColor};margin-top:2px;" title="推論隠蔽率: 再生中に裏で完了した推論の割合">${maskingRatio}% 隠蔽</div>`;
        }
        const ttfsCell = `
          <div style="text-align:right;white-space:nowrap;">
            <span style="font-weight:700;color:${ttfsColor};font-size:12px;">${turnTtfs} ms</span>
            ${ttfsBreakdown}
            ${queueWaitHtml}
            ${maskingHtml}
          </div>
        `;

        // スマートワーカーバッジ (マシン + エンジン)
        const sw = formatSmartWorker(item.worker_machine ? `${item.worker_machine} / ${item.worker_engine}` : item.worker_label, item.provider);
        const mBadgeBg = sw.machine.includes("2022") ? "#5865f2" : (sw.machine.includes("2017") ? "#9b59b6" : "#3ba55c");
        const workerBadges = `
          <div style="display:inline-flex;gap:4px;align-items:center;flex-wrap:nowrap;">
            <span class="badge" style="background:${mBadgeBg};color:white;font-size:10px;padding:2px 6px;border-radius:4px;font-weight:600;white-space:nowrap;">${escapeHtml(sw.machine)}</span>
            <span class="badge" style="background:rgba(255,255,255,0.08);color:#eee;font-size:10px;padding:2px 5px;border-radius:4px;white-space:nowrap;">${escapeHtml(sw.engine)}</span>
          </div>
        `;

        // 倍速 (RTF)
        const rtfVal = item.rtf || 0;
        const rtfSpeed = rtfVal > 0 ? (1 / rtfVal).toFixed(1) : 0;
        const rtfColor = rtfVal > 0 && rtfVal < 0.4 ? "#50e6ff" : (rtfVal < 1.0 ? "#a8ff78" : "#f39c12");
        const rtfCell = rtfVal > 0
          ? `<div style="text-align:right;font-family:monospace;white-space:nowrap;"><span style="color:${rtfColor};font-weight:700;">${rtfVal.toFixed(2)}x</span><div style="font-size:9px;color:var(--text-dim);">${rtfSpeed}倍速</div></div>`
          : `<div style="text-align:right;color:var(--text-dim);">--</div>`;

        // 余裕度 (Headroom 4段階インテリジェント表示)
        const hrVal = item.buffer_headroom_ms !== undefined ? item.buffer_headroom_ms : 0;
        let hrColor = "#a8ff78";
        let hrText = "";
        if (hrVal < 0) {
          hrColor = "#ed4245";
          hrText = `${hrVal}ms 🔴 途切れ`;
        } else if (hrVal < 200) {
          hrColor = "#f1c40f";
          hrText = `+${hrVal}ms ⚡ 僅少`;
        } else if (hrVal < 800) {
          hrColor = "#a8ff78";
          hrText = `+${hrVal}ms 🟢 安全`;
        } else {
          hrColor = "#50e6ff";
          hrText = `+${hrVal}ms 🛡️ 潤沢`;
        }

        const hasChunks = item.chunks && item.chunks.length > 0;
        const toggleIcon = hasChunks ? `<span style="cursor:pointer;color:#50e6ff;font-size:12px;" onclick="toggleLatencyDetail(${idx})">▶</span>` : '';

        let rowHtml = `
          <tr id="lat-row-${idx}" style="border-bottom:1px solid rgba(255,255,255,0.04);cursor:${hasChunks ? 'pointer' : 'default'};" ${hasChunks ? `onclick="toggleLatencyDetail(${idx})"` : ''}>
            <td style="padding:6px 8px;text-align:center;">${toggleIcon}</td>
            <td style="padding:6px 8px;color:var(--text-dim);font-family:monospace;white-space:nowrap;">${escapeHtml(item.timestamp)}</td>
            <td style="padding:6px 8px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
              <strong style="color:var(--text-normal);">${escapeHtml(item.user_name)}:</strong>
              <span style="color:var(--text-muted);">${escapeHtml(item.text_preview)}</span>
            </td>
            <td style="padding:6px 8px;color:var(--text-normal);white-space:nowrap;">${escapeHtml(item.speaker_name)}</td>
            <td style="padding:6px 8px;white-space:nowrap;">${workerBadges}</td>
            <td style="padding:6px 8px;">${ttfsCell}</td>
            <td style="padding:6px 8px;">${rtfCell}</td>
            <td style="padding:6px 8px;text-align:right;font-weight:600;color:${hrColor};white-space:nowrap;font-family:monospace;">${hrText}</td>
            <td style="padding:6px 8px;text-align:center;white-space:nowrap;">${statusBadge}</td>
          </tr>
        `;

        if (hasChunks) {
          const splitBadge = (item.split_ms !== undefined && item.split_ms > 0)
            ? `<span class="badge" style="background:rgba(255,255,255,0.08);color:#50e6ff;font-size:10px;padding:2px 6px;border-radius:4px;">⏱️ 形態素解析: ${item.split_ms}ms</span>`
            : '';
          let maskingBadge = '';
          if (item.is_cached) {
            maskingBadge = `<span class="badge" style="background:rgba(80,230,255,0.15);color:#50e6ff;font-size:10px;padding:2px 6px;border-radius:4px;font-weight:600;">⚡ キャッシュ即時再生</span>`;
          } else if (maskingRatio > 0 && item.chunks && item.chunks.length > 1) {
            const mBg = maskingRatio >= 100 ? 'rgba(46,204,113,0.15)' : (maskingRatio >= 50 ? 'rgba(241,196,15,0.15)' : 'rgba(237,66,69,0.15)');
            const mClr = maskingRatio >= 100 ? '#2ecc71' : (maskingRatio >= 50 ? '#f1c40f' : '#ed4245');
            maskingBadge = `<span class="badge" style="background:${mBg};color:${mClr};border:1px solid ${mClr}40;font-size:10px;padding:2px 6px;border-radius:4px;font-weight:600;">🛡️ 推論隠蔽率: ${maskingRatio}% ${maskingRatio >= 100 ? '(全パーツ先行完了)' : '(一部並行)'}</span>`;
          }
          const maxDur = Math.max(...item.chunks.map(c => (c.duration_ms || 1000)), 2000);

          rowHtml += `
            <tr id="lat-detail-${idx}" style="display:none;background:rgba(0,0,0,0.35);border-bottom:1px solid rgba(255,255,255,0.08);">
              <td colspan="9" style="padding:10px 16px 14px 28px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:6px;">
                  <div style="font-size:11px;font-weight:600;color:#50e6ff;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                    <span>🧩 パーツ別先行ストリーミング タイムライン内訳:</span>
                    ${splitBadge}
                    ${maskingBadge}
                  </div>
                  <div style="font-size:10px;color:var(--text-dim);">推論時間 (水色) vs 再生時間 (黄枠)</div>
                </div>
                <div style="display:flex;flex-direction:column;gap:6px;">
                  ${item.chunks.map((ch, cIdx) => {
                    const cRole = ch.role === 'name' ? '【名前先行】' : `【文 ${cIdx}】`;
                    const cMargin = ch.buffer_remaining_ms !== undefined ? ch.buffer_remaining_ms : (ch.buffer_headroom_ms !== undefined ? ch.buffer_headroom_ms : null);
                    let cMColor = 'var(--text-dim)';
                    let cMText = '--';
                    if (cMargin !== null) {
                      if (cMargin < 0) {
                        cMColor = '#ed4245';
                        cMText = `${Math.round(cMargin)}ms 途切れ (アンダーラン)`;
                      } else if (cMargin < 200) {
                        cMColor = '#f1c40f';
                        cMText = `+${Math.round(cMargin)}ms 余裕 (僅少)`;
                      } else if (cIdx === 0 && ch.role === 'name') {
                        cMColor = '#50e6ff';
                        cMText = `+${Math.round(cMargin)}ms (名前先行バッファ)`;
                      } else if (cIdx === 1 && prerollWait > 0) {
                        cMColor = '#a8ff78';
                        cMText = `+${Math.round(cMargin)}ms 余裕 (Preroll確保)`;
                      } else {
                        cMColor = '#a8ff78';
                        cMText = `+${Math.round(cMargin)}ms 余裕 (先行合成)`;
                      }
                    }
                    const sMs = ch.synthesis_ms || ch.synth_ms || 0;
                    const dMs = ch.duration_ms || 1000;
                    const cRtf = dMs > 0 ? (sMs / dMs).toFixed(2) : 0;
                    const synthWidthPct = Math.min(100, Math.max(5, (sMs / maxDur) * 100));
                    const playWidthPct = Math.min(100, Math.max(10, (dMs / maxDur) * 100));
                    const swChunk = formatSmartWorker(ch.worker_label || ch.worker, ch.provider);

                    return `
                      <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);border-radius:6px;padding:6px 10px;">
                        <div style="display:flex;align-items:center;gap:8px;font-size:11px;margin-bottom:4px;flex-wrap:wrap;">
                          <strong style="color:var(--text-normal);min-width:65px;">${cRole}</strong>
                          <span style="color:var(--text-muted);flex:1;min-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">「${escapeHtml(ch.text)}」</span>
                          <span class="badge" style="background:rgba(255,255,255,0.08);color:#ddd;font-size:9px;padding:1px 5px;">${escapeHtml(swChunk.machine)}</span>
                          <span style="color:#50e6ff;font-family:monospace;font-size:10px;">推論: ${sMs}ms</span>
                          <span style="color:#f1c40f;font-family:monospace;font-size:10px;">再生: ${dMs}ms</span>
                          <span style="color:#a8ff78;font-family:monospace;font-size:10px;font-weight:600;">(RTF: ${cRtf}x)</span>
                          <span style="color:${cMColor};font-weight:700;font-family:monospace;font-size:10px;">${cMText}</span>
                        </div>
                        <div style="height:6px;background:rgba(255,255,255,0.06);border-radius:3px;display:flex;overflow:hidden;position:relative;">
                          <div style="width:${synthWidthPct}%;background:#50e6ff;height:100%;border-radius:3px;" title="推論時間: ${sMs}ms"></div>
                          <div style="position:absolute;left:0;top:0;width:${playWidthPct}%;height:100%;border:1px dashed #f1c40f;pointer-events:none;" title="再生時間枠: ${dMs}ms"></div>
                        </div>
                      </div>
                    `;
                  }).join('')}
                </div>
              </td>
            </tr>
          `;
        }

        return rowHtml;
      }).join("");
    }
  }

  // 5. 直近15件の初動ラグ推移ミニグラフ (Sparkline) の描画
  renderLatencySparkline(history, summary);
}

// 直近15件の初動ラグ推移ミニグラフ (Sparkline SVG)
function renderLatencySparkline(history, summary) {
  const container = document.getElementById("latency-sparkline-container");
  if (!container) return;

  const sessionBadge = document.getElementById("latency-session-badge");
  if (sessionBadge && summary) {
    const sId = summary.session_id || 1;
    const sCnt = summary.session_record_count !== undefined ? summary.session_record_count : (history ? history.length : 0);
    const idleMin = summary.idle_seconds_ago ? Math.round(summary.idle_seconds_ago / 60) : 0;
    const idleInfo = idleMin > 0 ? ` (前回発話: ${idleMin}分前)` : '';
    sessionBadge.textContent = `セッション #${sId} (${sCnt}件)${idleInfo}`;
  }

  if (!history || history.length === 0) {
    container.innerHTML = '<span style="color:var(--text-dim);font-size:11px;">計測データ待機中...</span>';
    return;
  }

  // 最新15件を時系列（古い方から新しい方へ）に並べ替え
  const items = history.slice(0, 15).reverse();
  const n = items.length;
  if (n < 1) {
    container.innerHTML = '<span style="color:var(--text-dim);font-size:11px;">計測データ待機中...</span>';
    return;
  }

  const svgW = 600;
  const svgH = 95;
  const padL = 38;
  const padR = 25;
  const padT = 16;
  const padB = 22;
  const plotW = svgW - padL - padR;
  const plotH = svgH - padT - padB;

  const ttfses = items.map(h => (h.turn_ttfs_ms !== undefined ? h.turn_ttfs_ms : (h.ttfs_ms || h.total_latency_ms || 0)));
  const maxVal = Math.max(700, Math.max(...ttfses) * 1.15);

  const getY = val => Math.round(padT + plotH - (Math.min(val, maxVal) / maxVal) * plotH);
  const getX = idx => Math.round(padL + (n === 1 ? plotW / 2 : (idx / (n - 1)) * plotW));

  const y250 = getY(250);
  const y600 = getY(600);

  const points = items.map((h, i) => `${getX(i)},${getY(ttfses[i])}`).join(" ");

  let svg = `<svg viewBox="0 0 ${svgW} ${svgH}" style="width:100%;height:100%;display:block;" preserveAspectRatio="none">`;

  // 目標ライン 250ms (緑破線)
  svg += `<line x1="${padL}" y1="${y250}" x2="${svgW - padR}" y2="${y250}" stroke="#2ecc71" stroke-width="1" stroke-dasharray="4,3" opacity="0.6"/>`;
  svg += `<text x="${padL - 4}" y="${y250 + 3}" fill="#2ecc71" font-size="9" text-anchor="end" opacity="0.8">250ms</text>`;

  // 許容ライン 600ms (オレンジ破線)
  if (maxVal > 500) {
    svg += `<line x1="${padL}" y1="${y600}" x2="${svgW - padR}" y2="${y600}" stroke="#f39c12" stroke-width="1" stroke-dasharray="4,3" opacity="0.4"/>`;
    svg += `<text x="${padL - 4}" y="${y600 + 3}" fill="#f39c12" font-size="9" text-anchor="end" opacity="0.7">600ms</text>`;
  }

  // 折れ線
  if (n > 1) {
    svg += `<polyline points="${points}" fill="none" stroke="#5865f2" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>`;
  }

  // データ点 (サークル)
  items.forEach((h, i) => {
    const x = getX(i);
    const y = getY(ttfses[i]);
    const val = ttfses[i];
    const isCached = h.is_cached;
    const ptColor = isCached ? "#50e6ff" : (val < 250 ? "#a8ff78" : (val < 600 ? "#5865f2" : "#ed4245"));
    const origIdx = history.indexOf(h);
    const cVal = h.core_ttfs_ms !== undefined ? h.core_ttfs_ms : (h.turn_ttfs_ms !== undefined ? Math.max(0, h.turn_ttfs_ms - (h.preroll_wait_ms || 0)) : 0);
    const pVal = h.preroll_wait_ms || 0;
    const tip = `[${h.timestamp || ''}] ${escapeHtml(h.speaker_name || '')}: ${val}ms (Core: ${Math.round(cVal)}ms + Pre: ${Math.round(pVal)}ms) | ${isCached ? 'キャッシュ' : (h.worker_label || '推論')}`;

    svg += `<g style="cursor:pointer;" onclick="highlightLatencyRow(${origIdx})">
      <circle cx="${x}" cy="${y}" r="4.5" fill="${ptColor}" stroke="#1e1f22" stroke-width="1.5">
        <title>${tip}</title>
      </circle>
      <text x="${x}" y="${svgH - 6}" fill="var(--text-dim, #72767d)" font-size="8" text-anchor="middle">${escapeHtml(h.timestamp ? h.timestamp.slice(3) : '')}</text>
    </g>`;
  });

  svg += `</svg>`;
  container.innerHTML = svg;
}

// グラフの点をクリックしたときにテーブル行をハイライト
window.highlightLatencyRow = function(idx) {
  const row = document.getElementById(`lat-row-${idx}`);
  if (row) {
    row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    row.style.transition = 'background 0.2s';
    row.style.background = 'rgba(88,101,242,0.3)';
    setTimeout(() => {
      row.style.background = '';
    }, 1200);
  }
};

// レイテンシ集計手動リセット
window.resetLatencyMetrics = async function() {
  if (!confirm("レイテンシ集計をリセットしますか？\n（長時間のアイドル後に前回の数値を引きずらず、新しいセッションとして計測を開始できます）")) {
    return;
  }
  try {
    const res = await fetch(`/api/system/latency/reset?guild_id=${currentGuildId}`, {
      method: "POST"
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    showToast(data.message || "レイテンシ集計をリセットしました。", "success");
    if (typeof fetchSystemMetrics === 'function') {
      fetchSystemMetrics();
    }
  } catch (err) {
    showToast(`リセット失敗: ${err.message}`, "error");
  }
};

// 履歴行の展開トグル関数
window.toggleLatencyDetail = function(idx) {
  const detailRow = document.getElementById(`lat-detail-${idx}`);
  const row = document.getElementById(`lat-row-${idx}`);
  if (!detailRow) return;
  if (detailRow.style.display === "none") {
    detailRow.style.display = "table-row";
    if (row) {
      const icon = row.querySelector("td span");
      if (icon) icon.textContent = "▼";
    }
  } else {
    detailRow.style.display = "none";
    if (row) {
      const icon = row.querySelector("td span");
      if (icon) icon.textContent = "▶";
    }
  }
};

function renderCpuChart(history) {
  renderLineChart("metrics-cpu-chart", history, {
    yMin: 0,
    yMax: 100,
    yFormat: (val) => `${Math.round(val)}%`,
    thresholds: [
      { value: 70, color: "rgba(250, 166, 26, 0.7)", label: "70% 並列上限" },
      { value: 85, color: "rgba(237, 66, 69, 0.7)", label: "85% 退避" }
    ],
    series: METRICS_SERIES,
    getValue: (pt, key) => (pt[key] && typeof pt[key].cpu_percent === "number") ? pt[key].cpu_percent : null
  });
}

function renderMemoryChart(history) {
  // 履歴全体の最大メモリ(MB)を算出
  let maxMb = 500;
  if (history && history.length > 0) {
    history.forEach(pt => {
      METRICS_SERIES.forEach(s => {
        let mb = null;
        if (s.key === "host" && pt.host) mb = pt.host.memory_used_mb;
        else if (pt[s.key]) mb = pt[s.key].memory_mb;
        if (typeof mb === "number" && mb > maxMb) maxMb = mb;
      });
    });
  }
  // 15%マージン
  maxMb = Math.ceil(maxMb * 1.15);

  renderLineChart("metrics-mem-chart", history, {
    yMin: 0,
    yMax: maxMb,
    yFormat: (mb) => {
      if (mb >= 1024) return `${(mb / 1024).toFixed(1)}GB`;
      return `${Math.round(mb)}MB`;
    },
    thresholds: [],
    series: METRICS_SERIES,
    getValue: (pt, key) => {
      if (key === "host" && pt.host) return pt.host.memory_used_mb;
      if (pt[key] && typeof pt[key].memory_mb === "number") return pt[key].memory_mb;
      return null;
    }
  });
}

function renderWorkerCpuChart(history) {
  renderLineChart("metrics-cpu-chart", history, {
    yMin: 0,
    yMax: 100,
    yFormat: (val) => `${Math.round(val)}%`,
    thresholds: [
      { value: 70, color: "rgba(250, 166, 26, 0.7)", label: "70% 警告" },
      { value: 85, color: "rgba(237, 66, 69, 0.7)", label: "85% 高負荷" }
    ],
    series: WORKER_CPU_SERIES,
    getValue: (pt, key) => (pt && typeof pt[key] === "number") ? pt[key] : null
  });
}

function renderWorkerMemoryChart(history) {
  let maxGb = 16;
  if (history && history.length > 0) {
    history.forEach(pt => {
      if (typeof pt.mem_total_gb === "number" && pt.mem_total_gb > maxGb) maxGb = pt.mem_total_gb;
      if (typeof pt.gpu_mem_total_gb === "number" && pt.gpu_mem_total_gb > maxGb) maxGb = pt.gpu_mem_total_gb;
      if (typeof pt.mem_used_gb === "number" && pt.mem_used_gb * 1.2 > maxGb) maxGb = pt.mem_used_gb * 1.2;
    });
  }
  maxGb = Math.ceil(maxGb);

  renderLineChart("metrics-mem-chart", history, {
    yMin: 0,
    yMax: maxGb,
    yFormat: (gb) => `${gb.toFixed(1)}GB`,
    thresholds: [],
    series: WORKER_MEM_SERIES,
    getValue: (pt, key) => (pt && typeof pt[key] === "number") ? pt[key] : null
  });
}

function renderLineChart(canvasId, history, options) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = rect.width || canvas.parentElement.clientWidth || 360;
  const height = rect.height || 180;

  canvas.width = width * dpr;
  canvas.height = height * dpr;

  const ctx = canvas.getContext("2d");
  ctx.save();
  ctx.scale(dpr, dpr);

  // 余白設定
  const padLeft = 45;
  const padRight = 20;
  const padTop = 15;
  const padBottom = 25;
  const plotWidth = width - padLeft - padRight;
  const plotHeight = height - padTop - padBottom;

  ctx.clearRect(0, 0, width, height);

  if (!history || history.length === 0) {
    ctx.fillStyle = "#72767d";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("メトリクス収集中... (30秒間隔)", width / 2, height / 2);
    ctx.restore();
    return;
  }

  const { yMin, yMax, yFormat, thresholds, getValue } = options;
  const yRange = yMax - yMin || 1;

  // 1. 水平グリッド線 & Y軸目盛り (4分割)
  const gridSteps = 4;
  ctx.lineWidth = 1;
  ctx.strokeStyle = "rgba(255, 255, 255, 0.07)";
  ctx.fillStyle = "#72767d";
  ctx.font = "10px sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";

  for (let i = 0; i <= gridSteps; i++) {
    const ratio = i / gridSteps;
    const yVal = yMin + ratio * yRange;
    const y = padTop + plotHeight - ratio * plotHeight;

    ctx.beginPath();
    ctx.setLineDash([]);
    ctx.moveTo(padLeft, y);
    ctx.lineTo(padLeft + plotWidth, y);
    ctx.stroke();

    ctx.fillText(yFormat(yVal), padLeft - 6, y);
  }

  // 2. 閾値ガイドライン (CPUチャート等)
  if (thresholds && thresholds.length > 0) {
    thresholds.forEach(th => {
      if (th.value >= yMin && th.value <= yMax) {
        const ratio = (th.value - yMin) / yRange;
        const y = padTop + plotHeight - ratio * plotHeight;

        ctx.beginPath();
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = th.color;
        ctx.moveTo(padLeft, y);
        ctx.lineTo(padLeft + plotWidth, y);
        ctx.stroke();

        ctx.fillStyle = th.color;
        ctx.textAlign = "right";
        ctx.fillText(th.label, padLeft + plotWidth - 4, y - 6);
      }
    });
  }

  // 3. X軸時間目盛り (最初・中間・最後)
  ctx.setLineDash([]);
  ctx.fillStyle = "#72767d";
  ctx.font = "10px sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";

  const numPoints = history.length;
  const labelIndices = [];
  if (numPoints <= 3) {
    for (let i = 0; i < numPoints; i++) labelIndices.push(i);
  } else {
    labelIndices.push(0);
    if (numPoints > 6) labelIndices.push(Math.floor(numPoints / 2));
    labelIndices.push(numPoints - 1);
  }

  labelIndices.forEach(idx => {
    const pt = history[idx];
    if (!pt || !pt.time) return;
    const x = numPoints === 1
      ? padLeft + plotWidth / 2
      : padLeft + (idx / (numPoints - 1)) * plotWidth;
    ctx.fillText(pt.time, x, padTop + plotHeight + 6);
  });

  // 4. 各系列の折れ線描画
  const activeSeries = options.series || METRICS_SERIES;
  activeSeries.forEach(series => {
    let started = false;
    let lastX = null;
    let lastY = null;

    ctx.beginPath();
    if (series.dashed) {
      ctx.setLineDash([4, 4]);
    } else {
      ctx.setLineDash([]);
    }
    ctx.strokeStyle = series.color;
    ctx.lineWidth = series.width || 2;

    for (let i = 0; i < numPoints; i++) {
      const val = getValue(history[i], series.key);
      if (typeof val !== "number") continue;

      const x = numPoints === 1
        ? padLeft + plotWidth / 2
        : padLeft + (i / (numPoints - 1)) * plotWidth;
      const ratio = Math.max(0, Math.min(1, (val - yMin) / yRange));
      const y = padTop + plotHeight - ratio * plotHeight;

      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
      lastX = x;
      lastY = y;
    }

    if (started) {
      ctx.stroke();

      // 最新点に小さなサークルマーカーを描画
      if (lastX !== null && lastY !== null) {
        ctx.beginPath();
        ctx.setLineDash([]);
        ctx.fillStyle = series.color;
        ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  });

  ctx.restore();
}

window.addEventListener("resize", () => {
  if (latestMetricsData) {
    renderSystemMetricsUI(latestMetricsData);
  }
});

async function purgeCache(target = 'all') {
  let confirmMsg = "全ての音声キャッシュ（Cloud & VOICEVOX）を削除しますか？\n（次回の読み上げ時に自動再生成されます）";
  if (target === 'cloud') {
    confirmMsg = "クラウドTTS（Azure / Google）のキャッシュを削除しますか？";
  } else if (target === 'voicevox') {
    confirmMsg = "VOICEVOX専用キャッシュ（20GB）を削除しますか？";
  }
  if (!confirm(confirmMsg)) return;

  try {
    const res = await fetch(`/api/cache/purge?guild_id=${currentGuildId}&target=${target}`, { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      showToast(`キャッシュ (${data.target || target}) ${data.purged_files} 件をパージしました！`, "success");
      loadStats(currentGuildId);
    } else {
      showToast(data.error || "パージに失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  }
}

// --- キャッシュファイルエクスプローラー ---
let currentCeTarget = 'cloud';
let currentCePage = 1;
let currentCeSearch = '';
let ceSearchTimeout = null;
let currentPlayingAudio = null;
let currentPlayingBtn = null;

function switchCacheExplorerTarget(target) {
  currentCeTarget = target;
  currentCePage = 1;
  const cloudBtn = document.getElementById("ce-tab-cloud");
  const localBtn = document.getElementById("ce-tab-local");
  if (cloudBtn && localBtn) {
    if (target === 'cloud') {
      cloudBtn.className = "btn btn-sm btn-primary";
      localBtn.className = "btn btn-sm btn-secondary";
    } else {
      cloudBtn.className = "btn btn-sm btn-secondary";
      localBtn.className = "btn btn-sm btn-primary";
    }
  }
  loadCacheExplorerFiles();
}

function onCacheExplorerSearch() {
  if (ceSearchTimeout) clearTimeout(ceSearchTimeout);
  ceSearchTimeout = setTimeout(() => {
    const input = document.getElementById("ce-search-input");
    currentCeSearch = input ? input.value.trim() : "";
    currentCePage = 1;
    loadCacheExplorerFiles();
  }, 300);
}

function prevCacheExplorerPage() {
  if (currentCePage > 1) {
    currentCePage--;
    loadCacheExplorerFiles();
  }
}

function nextCacheExplorerPage() {
  currentCePage++;
  loadCacheExplorerFiles();
}

let ceSelectedFiles = new Set();

async function loadCacheExplorerFiles() {
  const tbody = document.getElementById("ce-file-list-tbody");
  const countBadge = document.getElementById("ce-count-badge");
  const pageIndicator = document.getElementById("ce-page-indicator");
  const paginationInfo = document.getElementById("ce-pagination-info");
  const prevBtn = document.getElementById("ce-prev-btn");
  const nextBtn = document.getElementById("ce-next-btn");
  const sortSelect = document.getElementById("ce-sort-select");
  const selectAllCb = document.getElementById("ce-select-all");

  // 選択状態のリセット
  ceSelectedFiles.clear();
  if (selectAllCb) selectAllCb.checked = false;
  updateCeBatchDeleteBtn();

  let sortBy = "mtime";
  let sortOrder = "desc";
  if (sortSelect && sortSelect.value) {
    const parts = sortSelect.value.split("_");
    sortBy = parts[0];
    sortOrder = parts[1] || "desc";
  }

  if (tbody) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:24px;color:var(--text-muted);">読み込み中... ⏳</td></tr>`;
  }

  try {
    const query = new URLSearchParams({
      guild_id: currentGuildId,
      target: currentCeTarget,
      page: currentCePage,
      per_page: 25,
      search: currentCeSearch,
      sort_by: sortBy,
      sort_order: sortOrder
    });
    const res = await fetch(`/api/cache/files?${query}`);
    if (res.status === 403) {
      if (tbody) tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:24px;color:#ed4245;">閲覧権限がありません</td></tr>`;
      return;
    }
    const data = await res.json();
    const files = data.files || [];
    const total = data.total || 0;
    const totalPages = data.total_pages || 1;
    currentCePage = data.page || 1;

    if (countBadge) countBadge.textContent = `${total}件`;
    if (pageIndicator) pageIndicator.textContent = `${currentCePage} / ${totalPages}`;
    if (prevBtn) prevBtn.disabled = (currentCePage <= 1);
    if (nextBtn) nextBtn.disabled = (currentCePage >= totalPages);

    const startIdx = total === 0 ? 0 : (currentCePage - 1) * (data.per_page || 25) + 1;
    const endIdx = Math.min(startIdx + files.length - 1, total);
    if (paginationInfo) paginationInfo.textContent = `${startIdx} - ${endIdx} / 全 ${total} 件`;

    if (!tbody) return;
    if (files.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:24px;color:var(--text-muted);">該当するキャッシュファイルはありません</td></tr>`;
      return;
    }

    tbody.innerHTML = "";
    files.forEach(f => {
      const tr = document.createElement("tr");
      tr.style.borderBottom = "1px solid rgba(255,255,255,0.05)";

      const isChunk = !!f.is_chunk;
      const typeBadge = isChunk
        ? `<span class="badge" style="background:#f0b232;color:#111;font-size:10px;font-weight:600;padding:2px 6px;">パーツ</span>`
        : `<span class="badge" style="background:#5865f2;color:white;font-size:10px;font-weight:600;padding:2px 6px;">全文</span>`;

      const hitBadge = (f.hit_count > 1)
        ? `<span class="badge" style="background:#23a55a;color:white;font-size:11px;padding:2px 6px;">${f.hit_count}回 🔥</span>`
        : `<span style="color:var(--text-dim);font-size:11px;">1回</span>`;

      const displayText = f.text
        ? escapeHtml(f.text)
        : `<span style="color:var(--text-dim);font-style:italic;">${escapeHtml(f.filename)}</span>`;

      const speakerDisplay = f.speaker_name && f.speaker_name !== "-"
        ? `<div style="font-weight:600;color:var(--text-normal);">${escapeHtml(f.speaker_name)}</div><div style="font-size:10px;color:var(--text-dim);">${escapeHtml(f.provider || '')}</div>`
        : `<span style="color:var(--text-dim);font-size:11px;">${escapeHtml(f.provider || '-')}</span>`;

      tr.innerHTML = `
        <td style="padding:8px 10px;text-align:center;">
          <input type="checkbox" class="ce-file-cb" value="${escapeHtml(f.filename)}" onchange="onCacheFileSelectChange('${escapeHtml(f.filename)}', this.checked)" style="cursor:pointer;">
        </td>
        <td style="padding:8px 10px;text-align:center;">
          <button type="button" class="btn btn-secondary btn-sm play-btn" style="padding:3px 8px;font-size:11px;" onclick="playCacheAudio('${currentCeTarget}', '${escapeHtml(f.filename)}', this)">▶</button>
        </td>
        <td style="padding:8px 12px;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(f.text || f.filename)}">
          ${displayText}
        </td>
        <td style="padding:8px 12px;font-size:12px;">
          ${speakerDisplay}
        </td>
        <td style="padding:8px 10px;text-align:center;">
          ${typeBadge}
        </td>
        <td style="padding:8px 10px;text-align:center;">
          ${hitBadge}
        </td>
        <td style="padding:8px 12px;color:var(--text-muted);font-size:12px;white-space:nowrap;">
          <div>${f.size_formatted}</div>
          <div style="font-size:10px;color:var(--text-dim);">${f.duration_str || '-'}</div>
        </td>
        <td style="padding:8px 12px;color:var(--text-muted);font-size:11px;white-space:nowrap;">
          ${f.mtime_str}
        </td>
        <td style="padding:8px 12px;text-align:right;white-space:nowrap;">
          <a href="/api/cache/audio/${currentCeTarget}/${encodeURIComponent(f.filename)}?download=1" download="${f.filename}" class="btn btn-secondary btn-sm" style="padding:3px 7px;font-size:11px;text-decoration:none;margin-right:4px;" title="WAVダウンロード">📥</a>
          <button type="button" class="btn btn-danger btn-sm" style="padding:3px 7px;font-size:11px;" onclick="deleteCacheFile('${currentCeTarget}', '${escapeHtml(f.filename)}')" title="削除">🗑️</button>
        </td>
      `;
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error("Failed to load cache files:", e);
    if (tbody) tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:24px;color:#ed4245;">一覧の取得に失敗しました</td></tr>`;
  }
}

function onCacheFileSelectChange(filename, checked) {
  if (checked) {
    ceSelectedFiles.add(filename);
  } else {
    ceSelectedFiles.delete(filename);
  }
  updateCeBatchDeleteBtn();
}
window.onCacheFileSelectChange = onCacheFileSelectChange;

function toggleSelectAllCacheFiles(checked) {
  const cbs = document.querySelectorAll(".ce-file-cb");
  cbs.forEach(cb => {
    cb.checked = checked;
    if (checked) {
      ceSelectedFiles.add(cb.value);
    } else {
      ceSelectedFiles.delete(cb.value);
    }
  });
  updateCeBatchDeleteBtn();
}
window.toggleSelectAllCacheFiles = toggleSelectAllCacheFiles;

function updateCeBatchDeleteBtn() {
  const btn = document.getElementById("ce-batch-delete-btn");
  if (!btn) return;
  const count = ceSelectedFiles.size;
  if (count > 0) {
    btn.style.display = "inline-flex";
    btn.disabled = false;
    btn.textContent = `🗑️ 選択した ${count}件を削除`;
  } else {
    btn.style.display = "none";
    btn.disabled = true;
    btn.textContent = "🗑️ 選択した 0件を削除";
  }
}

async function deleteSelectedCacheFiles() {
  const count = ceSelectedFiles.size;
  if (count === 0) return;
  if (!confirm(`選択された ${count} 件のキャッシュファイルをストレージから削除しますか？`)) {
    return;
  }

  const btn = document.getElementById("ce-batch-delete-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "削除中...";
  }

  try {
    const res = await fetch("/api/cache/delete_batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: currentGuildId,
        target: currentCeTarget,
        filenames: Array.from(ceSelectedFiles)
      })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`${data.deleted_count} 件のキャッシュファイルを削除しました`, "success");
      ceSelectedFiles.clear();
      loadCacheExplorerFiles();
      loadStats(currentGuildId);
    } else {
      showToast(data.error || "一括削除に失敗しました", "error");
    }
  } catch (e) {
    console.error("Batch delete error:", e);
    showToast("一括削除中に通信エラーが発生しました", "error");
  } finally {
    updateCeBatchDeleteBtn();
  }
}
window.deleteSelectedCacheFiles = deleteSelectedCacheFiles;

function playCacheAudio(target, filename, btnEl) {
  if (currentPlayingAudio) {
    currentPlayingAudio.pause();
    currentPlayingAudio = null;
    if (currentPlayingBtn) {
      currentPlayingBtn.textContent = "▶ 試聴";
      currentPlayingBtn.className = "btn btn-secondary btn-sm play-btn";
    }
    if (currentPlayingBtn === btnEl) {
      currentPlayingBtn = null;
      return;
    }
  }

  const audioUrl = `/api/cache/audio/${target}/${encodeURIComponent(filename)}?t=${Date.now()}`;
  const audio = new Audio(audioUrl);
  btnEl.textContent = "⏹ 停止";
  btnEl.className = "btn btn-primary btn-sm play-btn";
  currentPlayingAudio = audio;
  currentPlayingBtn = btnEl;

  audio.onended = () => {
    btnEl.textContent = "▶ 試聴";
    btnEl.className = "btn btn-secondary btn-sm play-btn";
    currentPlayingAudio = null;
    currentPlayingBtn = null;
  };
  audio.onerror = () => {
    showToast("音声再生に失敗しました", "error");
    btnEl.textContent = "▶ 試聴";
    btnEl.className = "btn btn-secondary btn-sm play-btn";
    currentPlayingAudio = null;
    currentPlayingBtn = null;
  };

  audio.play().catch(e => {
    console.error("Audio play error:", e);
    showToast("再生を開始できませんでした", "error");
    btnEl.textContent = "▶ 試聴";
    btnEl.className = "btn btn-secondary btn-sm play-btn";
    currentPlayingAudio = null;
    currentPlayingBtn = null;
  });
}

async function deleteCacheFile(target, filename) {
  if (!confirm(`キャッシュファイル「${filename}」を削除しますか？`)) return;

  try {
    const res = await fetch(`/api/cache/file/${target}/${encodeURIComponent(filename)}?guild_id=${currentGuildId}`, {
      method: "DELETE"
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`キャッシュファイルを削除しました (${filename})`, "success");
      loadCacheExplorerFiles();
      loadStats(currentGuildId);
    } else {
      showToast(data.error || "削除に失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  }
}

// --- 音声モデル & エンジン更新管理 ---
let modelDownloadPollTimer = null;

async function checkModelsStatus() {
  const btn = document.getElementById("check-models-btn");
  const totalBadge = document.getElementById("model-updater-total-badge");
  const tbody = document.getElementById("coeiroink-models-tbody");
  const chipsContainer = document.getElementById("engine-status-chips");

  if (btn) {
    btn.disabled = true;
    btn.textContent = "確認中... ⏳";
  }

  try {
    const res = await fetch(`/api/system/models/status?guild_id=${currentGuildId}`);
    if (res.status === 403) return;
    const data = await res.json();

    // 1. バッジ更新
    const vCounts = data.voice_counts || {};
    if (totalBadge) {
      totalBadge.textContent = `全 ${vCounts.total || 0} モデル登録済`;
    }

    // 2. エンジンステータスチップ
    if (chipsContainer && data.engines) {
      chipsContainer.innerHTML = "";
      const engineConfigs = [
        { key: "azure", label: "Azure Speech", badgeColor: "#0078d4", count: vCounts.azure || 0 },
        { key: "google", label: "Google Cloud TTS", badgeColor: "#ea4335", count: vCounts.google || 0 },
        { key: "edge", label: "Edge-TTS", badgeColor: "#107c41", count: vCounts.edge || 0 },
        { key: "voicevox", label: "VOICEVOX", badgeColor: "#23a55a", count: vCounts.voicevox || 0 },
        { key: "sharevox", label: "SHAREVOX", badgeColor: "#f0b232", count: vCounts.sharevox || 0 },
        { key: "coeiroink", label: "COEIROINK", badgeColor: "#eb459e", count: vCounts.coeiroink || 0 },
        { key: "openjtalk", label: "OpenJTalk", badgeColor: "#0ea5e9", count: vCounts.openjtalk || 0 },
      ];

      engineConfigs.forEach(cfg => {
        const eng = data.engines[cfg.key] || {};
        const isOnline = eng.available;
        const chip = document.createElement("div");
        chip.style.cssText = "display:flex;align-items:center;gap:6px;background:rgba(0,0,0,0.25);padding:6px 12px;border-radius:20px;font-size:12px;border:1px solid rgba(255,255,255,0.06);";
        chip.innerHTML = `
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${isOnline ? '#23a55a' : '#ed4245'};"></span>
          <strong>${cfg.label}</strong>:
          <span class="badge" style="background:${cfg.badgeColor};color:white;font-size:10px;padding:2px 6px;">${cfg.count}話者</span>
          <span style="color:var(--text-dim);font-size:11px;">(${isOnline ? '稼働中' : '停止中'})</span>
        `;
        chipsContainer.appendChild(chip);
      });
    }

    // 3. COEIROINK公認モデル一覧
    const coe = data.coeiroink || {};
    const speakers = coe.speakers || [];
    if (tbody) {
      if (speakers.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--text-muted);">モデル情報を取得できませんでした</td></tr>`;
      } else {
        tbody.innerHTML = "";
        speakers.forEach(sp => {
          const tr = document.createElement("tr");
          tr.style.borderBottom = "1px solid rgba(255,255,255,0.05)";

          let statusBadge = `<span class="badge" style="background:#23a55a;color:white;">導入済み</span>`;
          let actionBtn = `<button type="button" class="btn btn-secondary btn-sm" style="padding:3px 8px;font-size:11px;" onclick="triggerDownloadSpeaker('${sp.prefix}', '${sp.speakerName}')">再取得 🔄</button>`;

          if (!sp.is_installed) {
            statusBadge = `<span class="badge" style="background:#ed4245;color:white;">未導入</span>`;
            actionBtn = `<button type="button" class="btn btn-primary btn-sm" style="padding:3px 8px;font-size:11px;" onclick="triggerDownloadSpeaker('${sp.prefix}', '${sp.speakerName}')">ダウンロード 📥</button>`;
          } else if (!sp.is_complete) {
            statusBadge = `<span class="badge" style="background:#f0b232;color:white;">一部未導入</span>`;
            actionBtn = `<button type="button" class="btn btn-primary btn-sm" style="padding:3px 8px;font-size:11px;" onclick="triggerDownloadSpeaker('${sp.prefix}', '${sp.speakerName}')">追加スタイル取得 📥</button>`;
          }

          tr.innerHTML = `
            <td style="padding:8px 12px;"><strong>${escapeHtml(sp.speakerName)}</strong></td>
            <td style="padding:8px 12px;color:var(--text-muted);font-family:monospace;font-size:11px;">v${escapeHtml(sp.version)}</td>
            <td style="padding:8px 12px;">
              <span style="font-weight:600;">${sp.installed_styles_count}</span> / ${sp.total_styles_count} スタイル
            </td>
            <td style="padding:8px 12px;">${statusBadge}</td>
            <td style="padding:8px 12px;text-align:right;">${actionBtn}</td>
          `;
          tbody.appendChild(tr);
        });
      }
    }

    // ダウンロード中ならポーリング開始
    if (coe.progress && coe.progress.is_running) {
      startModelDownloadPolling();
    }
  } catch (e) {
    console.error("Failed to check models status:", e);
    showToast("モデル状態の確認に失敗しました", "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "最新状態を確認 🔍";
    }
  }
}

async function triggerDownloadSpeaker(prefix, spName) {
  if (!confirm(`キャラクター「${spName}」の全スタイルをダウンロード・配置しますか？`)) return;

  try {
    const res = await fetch(`/api/system/models/update?guild_id=${currentGuildId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "speaker", prefix: prefix })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`「${spName}」のダウンロードを開始しました！`, "success");
      startModelDownloadPolling();
    } else {
      showToast(data.error || "ダウンロードの開始に失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  }
}

async function triggerDownloadAllModels() {
  if (!confirm("COEIROINKの全公認モデル（全14キャラクター・78スタイル）を一括ダウンロードしますか？\n（バックグラウンドで実行されます）")) return;

  try {
    const res = await fetch(`/api/system/models/update?guild_id=${currentGuildId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "all" })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast("公認モデルの一括ダウンロードを開始しました！", "success");
      startModelDownloadPolling();
    } else {
      showToast(data.error || "開始に失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  }
}

function startModelDownloadPolling() {
  const container = document.getElementById("model-download-progress-container");
  if (container) container.style.display = "block";

  if (modelDownloadPollTimer) clearInterval(modelDownloadPollTimer);

  modelDownloadPollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/system/models/progress?guild_id=${currentGuildId}`);
      if (!res.ok) return;
      const p = await res.json();

      const msgEl = document.getElementById("model-progress-msg");
      const pctEl = document.getElementById("model-progress-pct");
      const fillEl = document.getElementById("model-progress-fill");

      if (msgEl) msgEl.textContent = p.message || "ダウンロード処理中...";
      if (pctEl) pctEl.textContent = `${p.percent || 0}%`;
      if (fillEl) fillEl.style.width = `${p.percent || 0}%`;

      if (!p.is_running) {
        clearInterval(modelDownloadPollTimer);
        modelDownloadPollTimer = null;
        if (p.error) {
          showToast(`ダウンロード中にエラーが発生しました: ${p.error}`, "error");
        } else {
          showToast("モデルのダウンロードと反映が完了しました！🎉", "success");
          loadVoices();
          checkModelsStatus();
        }
        setTimeout(() => {
          if (container) container.style.display = "none";
        }, 4000);
      }
    } catch (e) {
      console.error("Progress poll error:", e);
    }
  }, 1500);
}

// --- MYCOEIROINK 配布音声ライブラリ管理 ---
let currentMyCoePage = 1;
let currentMyCoeTotalPages = 1;
let currentMyCoeSearchTimer = null;
let currentMyCoeInstalledUuids = new Set();
let myCoeSampleAudio = null;

async function loadMyCoeiroinkList(page = 1) {
  currentMyCoePage = page;
  const container = document.getElementById("mycoe-cards-grid");
  const searchInput = document.getElementById("mycoe-search-input");
  const filterSelect = document.getElementById("mycoe-filter-installed");
  const totalBadge = document.getElementById("mycoe-total-badge");
  const installedCountBadge = document.getElementById("mycoe-installed-count-badge");
  const syncedTimeEl = document.getElementById("mycoe-synced-time");
  const pageInfo = document.getElementById("mycoe-page-info");
  const prevBtn = document.getElementById("mycoe-prev-btn");
  const nextBtn = document.getElementById("mycoe-next-btn");

  const query = searchInput ? searchInput.value.trim() : "";

  if (container) {
    container.innerHTML = `<div style="text-align:center;padding:30px;color:var(--text-muted);grid-column:1/-1;">データを取得中... ⏳</div>`;
  }

  try {
    const url = `/api/mycoeiroink/list?page=${page}&limit=24&q=${encodeURIComponent(query)}`;
    const res = await fetch(url);
    if (!res.ok) {
      if (container) container.innerHTML = `<div style="text-align:center;padding:30px;color:#ff4757;grid-column:1/-1;">データの取得に失敗しました</div>`;
      return;
    }

    const normUuid = (u) => {
      if (!u) return "";
      const s = String(u).trim().toLowerCase().replace(/-/g, "");
      if (s.length === 32) {
        return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`;
      }
      return String(u).trim().toLowerCase();
    };

    const data = await res.json();
    currentMyCoeTotalPages = data.total_pages || 1;
    currentMyCoeInstalledUuids = new Set((data.installed_uuids || []).map(u => normUuid(u)));

    if (totalBadge) {
      totalBadge.textContent = `${data.total} キャラクター`;
    }
    if (installedCountBadge) {
      installedCountBadge.style.display = "inline-block";
      installedCountBadge.textContent = `導入済: ${currentMyCoeInstalledUuids.size}`;
    }
    if (syncedTimeEl && data.last_synced_at) {
      const dt = new Date(data.last_synced_at * 1000);
      syncedTimeEl.textContent = `最終同期: ${dt.toLocaleDateString("ja-JP")} ${dt.toLocaleTimeString("ja-JP", {hour:"2-digit", minute:"2-digit"})}`;
    }
    if (pageInfo) {
      pageInfo.textContent = `${data.page} / ${currentMyCoeTotalPages} ページ (${data.total}件)`;
    }
    if (prevBtn) prevBtn.disabled = data.page <= 1;
    if (nextBtn) nextBtn.disabled = data.page >= currentMyCoeTotalPages;

    let items = data.items || [];
    const filter = filterSelect ? filterSelect.value : "all";
    if (filter === "installed") {
      items = items.filter(it => currentMyCoeInstalledUuids.has(normUuid(it.speaker_uuid)));
    } else if (filter === "not_installed") {
      items = items.filter(it => !currentMyCoeInstalledUuids.has(normUuid(it.speaker_uuid)));
    }

    renderMyCoeCards(items);
  } catch (e) {
    console.error("loadMyCoeiroinkList error:", e);
    if (container) container.innerHTML = `<div style="text-align:center;padding:30px;color:#ff4757;grid-column:1/-1;">通信エラーが発生しました</div>`;
  }
}

function renderMyCoeCards(items) {
  const container = document.getElementById("mycoe-cards-grid");
  if (!container) return;

  const normUuid = (u) => {
    if (!u) return "";
    const s = String(u).trim().toLowerCase().replace(/-/g, "");
    if (s.length === 32) {
      return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`;
    }
    return String(u).trim().toLowerCase();
  };

  if (items.length === 0) {
    container.innerHTML = `<div style="text-align:center;padding:40px;color:var(--text-muted);grid-column:1/-1;">該当するキャラクターが見つかりませんでした</div>`;
    return;
  }

  container.innerHTML = items.map(it => {
    const isInstalled = currentMyCoeInstalledUuids.has(normUuid(it.speaker_uuid));
    const portraitHtml = it.portrait_url ? 
      `<img src="${escapeHtml(it.portrait_url)}" alt="${escapeHtml(it.speaker_name)}" style="width:52px;height:52px;border-radius:8px;object-fit:cover;background:rgba(0,0,0,0.3);flex-shrink:0;">` :
      `<div style="width:52px;height:52px;border-radius:8px;background:rgba(236,72,153,0.2);display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0;">🎙️</div>`;

    const statusBadge = isInstalled ? 
      `<span class="badge" style="background:#23a55a;color:white;font-size:10px;">🟢 導入済み</span>` :
      `<span class="badge" style="background:rgba(255,255,255,0.1);color:var(--text-muted);font-size:10px;">⚪ 未導入</span>`;

    // サンプル音声ボタン
    let samplesHtml = "";
    if (it.audio_samples && Object.keys(it.audio_samples).length > 0) {
      const sampleEntries = Object.entries(it.audio_samples);
      const firstSample = sampleEntries[0];
      const styleName = firstSample[0];
      const audioUrl = firstSample[1][0];
      if (audioUrl) {
        samplesHtml = `
          <button type="button" class="btn btn-secondary btn-sm" onclick="playMyCoeSample('${escapeHtml(audioUrl)}', this)" style="padding:2px 8px;font-size:11px;display:inline-flex;align-items:center;gap:4px;">
            <span>▶ 試聴 (${escapeHtml(styleName)})</span>
          </button>
        `;
      }
    }

    // スタイルバッジ
    const styles = it.styles || [];
    let stylesHtml = "";
    if (styles.length > 0) {
      const displayStyles = styles.slice(0, 3).map(s => `<span class="badge" style="background:rgba(255,255,255,0.06);font-size:10px;color:var(--text-muted);">${escapeHtml(s.style_name)}</span>`).join("");
      const moreCount = styles.length - 3;
      const moreHtml = moreCount > 0 ? `<span class="badge" style="background:rgba(255,255,255,0.06);font-size:10px;color:var(--text-dim);">+${moreCount}</span>` : "";
      stylesHtml = `<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:6px;">${displayStyles}${moreHtml}</div>`;
    }

        // 操作ボタン群 (自動導入 / 手動配布元 / ZIPアップロード)
        let actionsHtml = "";
        if (isInstalled) {
          actionsHtml = `
            <div style="display:flex;gap:4px;align-items:center;margin-left:auto;">
              <span style="font-size:11px;color:#23a55a;font-weight:600;">✓ 利用可能</span>
              <button type="button" class="btn btn-secondary btn-sm" onclick="openMyCoeUploadModal('${escapeHtml(it.speaker_uuid)}', '${escapeHtml(it.speaker_name)}')" style="padding:2px 8px;font-size:11px;" title="最新版のZIPで上書き更新">📁 更新</button>
            </div>
          `;
        } else if (it.auto_install_failed || it.is_manual_source) {
          actionsHtml = `
            <div style="display:flex;gap:4px;align-items:center;margin-left:auto;">
              ${it.url ? `<a href="${escapeHtml(it.url)}" target="_blank" rel="noopener" class="btn btn-secondary btn-sm" style="padding:2px 8px;font-size:11px;text-decoration:none;" title="BOOTH等の配布元を開く">🌐 配布先 ↗</a>` : ""}
              <button type="button" class="btn btn-primary btn-sm" onclick="openMyCoeUploadModal('${escapeHtml(it.speaker_uuid)}', '${escapeHtml(it.speaker_name)}')" style="padding:2px 8px;font-size:11px;" title="ダウンロードしたZIPを手動アップロード">📁 ZIP導入</button>
            </div>
          `;
        } else {
          actionsHtml = `
            <div style="display:flex;gap:4px;align-items:center;margin-left:auto;">
              <button type="button" class="btn btn-primary btn-sm" onclick="tryAutoInstallMyCoe('${escapeHtml(it.speaker_uuid)}', this)" style="padding:2px 8px;font-size:11px;background:#ec4899;border-color:#ec4899;" title="直リンクや共有URLから自動取得を試みる">⚡ 自動導入</button>
              <button type="button" class="btn btn-secondary btn-sm" onclick="openMyCoeUploadModal('${escapeHtml(it.speaker_uuid)}', '${escapeHtml(it.speaker_name)}')" style="padding:2px 8px;font-size:11px;" title="手動でZIPをアップロード">📁 ZIP</button>
            </div>
          `;
        }

        return `
          <div style="background:var(--bg-card-hover);border:1px solid ${isInstalled ? '#23a55a' : 'var(--border-color)'};border-radius:8px;padding:12px;display:flex;flex-direction:column;justify-content:space-between;transition:transform 0.15s, border-color 0.2s;" onmouseover="this.style.borderColor='#ec4899'" onmouseout="this.style.borderColor='${isInstalled ? '#23a55a' : 'var(--border-color)'}'">
            <div>
              <div style="display:flex;gap:10px;align-items:flex-start;">
                ${portraitHtml}
                <div style="flex:1;min-width:0;">
                  <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px;">
                    <h4 style="margin:0;font-size:14px;font-weight:700;color:var(--text-main);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                      ${escapeHtml(it.speaker_name)}
                    </h4>
                    ${statusBadge}
                  </div>
                  <div style="font-size:11px;color:var(--text-dim);margin-top:2px;">
                    ${escapeHtml(it.kana || "")}
                  </div>
                  <div style="font-size:10px;color:var(--text-dim);margin-top:4px;font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;" title="クリックでUUIDをコピー" onclick="copyMyCoeUuid('${escapeHtml(it.speaker_uuid)}')">
                    UUID: ${escapeHtml(it.speaker_uuid)} 📋
                  </div>
                </div>
              </div>
              ${stylesHtml}
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.06);gap:6px;">
              ${samplesHtml}
              ${actionsHtml}
            </div>
          </div>
        `;
      }).join("");
    }

function onMyCoeSearchInput() {
  if (currentMyCoeSearchTimer) clearTimeout(currentMyCoeSearchTimer);
  currentMyCoeSearchTimer = setTimeout(() => {
    loadMyCoeiroinkList(1);
  }, 350);
}

function onMyCoeFilterChange() {
  loadMyCoeiroinkList(1);
}

function changeMyCoePage(delta) {
  const newPage = currentMyCoePage + delta;
  if (newPage >= 1 && newPage <= currentMyCoeTotalPages) {
    loadMyCoeiroinkList(newPage);
  }
}

async function syncMyCoeiroink() {
  const btn = document.getElementById("mycoe-sync-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "同期中... ⏳";
  }
  try {
    const res = await fetch("/api/mycoeiroink/sync", { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      showToast(`MYCOEIROINK情報を同期しました！（全 ${data.total_count} キャラクター）🎉`, "success");
      loadMyCoeiroinkList(1);
    } else {
      showToast(data.error || "同期に失敗しました", "error");
    }
  } catch (e) {
    console.error("syncMyCoeiroink error:", e);
    showToast("通信エラーが発生しました", "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "🔄 公式配布情報を一括同期";
    }
  }
}

function playMyCoeSample(audioUrl, btnEl) {
  if (!myCoeSampleAudio) {
    myCoeSampleAudio = new Audio();
  }

  if (!myCoeSampleAudio.paused && myCoeSampleAudio.src === audioUrl) {
    myCoeSampleAudio.pause();
    if (btnEl) btnEl.innerHTML = btnEl.innerHTML.replace("⏸️", "▶");
    return;
  }

  myCoeSampleAudio.src = audioUrl;
  myCoeSampleAudio.play().then(() => {
    if (btnEl) {
      const origHtml = btnEl.innerHTML;
      btnEl.innerHTML = btnEl.innerHTML.replace("▶", "⏸️");
      myCoeSampleAudio.onended = () => {
        btnEl.innerHTML = origHtml;
      };
      myCoeSampleAudio.onpause = () => {
        btnEl.innerHTML = origHtml;
      };
    }
  }).catch(e => {
    console.error("Audio preview failed:", e);
    showToast("サンプル音声の再生に失敗しました", "error");
  });
}

function copyMyCoeUuid(uuid) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(uuid).then(() => {
      showToast(`UUIDをコピーしました: ${uuid}`, "info");
    });
  } else {
    prompt("UUID:", uuid);
  }
}

// 自動導入の試行
async function tryAutoInstallMyCoe(uuid, btnEl) {
  if (!uuid) return;
  const originalText = btnEl ? btnEl.textContent : "";
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "導入試行中... ⏳";
  }

  try {
    const res = await fetch("/api/mycoeiroink/install_auto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speaker_uuid: uuid })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(data.message || "キャラクターの自動導入に成功しました！🎉", "success");
      await loadMyCoeiroinkList(currentMyCoePage);
      if (typeof loadVoices === "function") loadVoices();
    } else {
      showToast(data.message || data.error || "自動導入に失敗しました。手動でZIPをアップロードしてください。", "warning");
      await loadMyCoeiroinkList(currentMyCoePage);
    }
  } catch (e) {
    console.error("tryAutoInstallMyCoe error:", e);
    showToast("通信エラーが発生しました", "error");
  } finally {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = originalText || "⚡ 自動導入";
    }
  }
}

// ZIP手動アップロードモーダルの制御
let currentUploadSpeakerUuid = null;

function openMyCoeUploadModal(speakerUuid = null, speakerName = null) {
  currentUploadSpeakerUuid = speakerUuid || null;
  const modal = document.getElementById("mycoe-upload-modal");
  const infoBox = document.getElementById("mycoe-target-speaker-info");
  const nameEl = document.getElementById("mycoe-target-speaker-name");
  const uuidEl = document.getElementById("mycoe-target-speaker-uuid");
  const progressContainer = document.getElementById("mycoe-upload-progress-container");
  const progressBar = document.getElementById("mycoe-upload-progress-bar");
  const statusText = document.getElementById("mycoe-upload-status-text");

  if (progressContainer) progressContainer.style.display = "none";
  if (progressBar) {
    progressBar.style.width = "0%";
    progressBar.style.background = "#ec4899";
  }
  if (statusText) statusText.textContent = "アップロード・安全検証中...";

  if (speakerUuid && speakerName) {
    if (infoBox) infoBox.style.display = "block";
    if (nameEl) nameEl.textContent = speakerName;
    if (uuidEl) uuidEl.textContent = speakerUuid;
  } else {
    if (infoBox) infoBox.style.display = "none";
  }

  if (modal) modal.style.display = "flex";
}

function closeMyCoeUploadModal(event) {
  if (event && event.target && event.target.id !== "mycoe-upload-modal") return;
  const modal = document.getElementById("mycoe-upload-modal");
  if (modal) modal.style.display = "none";
  currentUploadSpeakerUuid = null;
}

function onMyCoeDragOver(e) {
  e.preventDefault();
  const zone = document.getElementById("mycoe-drop-zone");
  if (zone) {
    zone.style.borderColor = "#ec4899";
    zone.style.background = "rgba(236,72,153,0.15)";
  }
}

function onMyCoeDragLeave(e) {
  e.preventDefault();
  const zone = document.getElementById("mycoe-drop-zone");
  if (zone) {
    zone.style.borderColor = "rgba(255,255,255,0.2)";
    zone.style.background = "rgba(0,0,0,0.2)";
  }
}

function onMyCoeDrop(e) {
  e.preventDefault();
  onMyCoeDragLeave(e);
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
    uploadMyCoeZip(e.dataTransfer.files[0]);
  }
}

function onMyCoeFileSelected(files) {
  if (files && files.length > 0) {
    uploadMyCoeZip(files[0]);
  }
}

async function uploadMyCoeZip(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".zip")) {
    showToast("アップロードできるのはZIP形式のファイルのみです (.zip)", "error");
    return;
  }
  if (file.size > 500 * 1024 * 1024) {
    showToast("ファイルサイズが上限 (500MB) を超過しています", "error");
    return;
  }

  const progressContainer = document.getElementById("mycoe-upload-progress-container");
  const progressBar = document.getElementById("mycoe-upload-progress-bar");
  const statusText = document.getElementById("mycoe-upload-status-text");
  const percentText = document.getElementById("mycoe-upload-percent-text");

  if (progressContainer) progressContainer.style.display = "block";
  if (progressBar) {
    progressBar.style.width = "5%";
    progressBar.style.background = "#ec4899";
  }

  // Cloudflare Tunnel の 100MB 制限を突破するため、20MB ごとのチャンクに分割して順次送信
  const CHUNK_SIZE = 20 * 1024 * 1024; // 20MB
  const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
  const uploadId = "up_" + Date.now() + "_" + Math.random().toString(36).substring(2, 9);
  const speakerUuid = currentUploadSpeakerUuid || "";

  console.log(`[MYCOEIROINK] チャンク分割アップロード開始: ${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB, 全${totalChunks}パート)`);

  for (let i = 0; i < totalChunks; i++) {
    const start = i * CHUNK_SIZE;
    const end = Math.min(start + CHUNK_SIZE, file.size);
    const chunkBlob = file.slice(start, end);

    const formData = new FormData();
    formData.append("upload_id", uploadId);
    formData.append("chunk_index", i.toString());
    formData.append("total_chunks", totalChunks.toString());
    formData.append("filename", file.name);
    formData.append("speaker_uuid", speakerUuid);
    formData.append("chunk", chunkBlob, file.name);

    const chunkPctBase = Math.round((start / file.size) * 85);
    if (statusText) {
      statusText.textContent = `送信中... (${(start / 1024 / 1024).toFixed(1)} / ${(file.size / 1024 / 1024).toFixed(1)} MB - パート ${i + 1}/${totalChunks})`;
    }
    if (progressBar) progressBar.style.width = `${chunkPctBase}%`;
    if (percentText) percentText.textContent = `${chunkPctBase}%`;

    try {
      const res = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/mycoeiroink/upload/chunk", true);

        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) {
            const currentBytes = start + e.loaded;
            const pct = Math.min(85, Math.round((currentBytes / file.size) * 85));
            if (progressBar) progressBar.style.width = `${pct}%`;
            if (percentText) percentText.textContent = `${pct}%`;
            if (statusText) {
              statusText.textContent = `送信中... (${(currentBytes / 1024 / 1024).toFixed(1)} / ${(file.size / 1024 / 1024).toFixed(1)} MB - パート ${i + 1}/${totalChunks})`;
            }
          }
        };

        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            try {
              resolve(JSON.parse(xhr.responseText));
            } catch (err) {
              reject(new Error("サーバー応答の解析に失敗しました"));
            }
          } else {
            let msg = `HTTPエラー (${xhr.status})`;
            try {
              const errObj = JSON.parse(xhr.responseText);
              if (errObj.error) msg = errObj.error;
            } catch (_) {
              if (xhr.status === 413) {
                msg = "Cloudflareの転送サイズ上限によりブロックされました";
              }
            }
            reject(new Error(msg));
          }
        };

        xhr.onerror = () => reject(new Error("通信エラーが発生しました"));
        xhr.send(formData);
      });

      // 最終チャンクのレスポンス処理
      if (i + 1 === totalChunks) {
        if (statusText) statusText.textContent = "全パート受信完了！サーバーで安全検証・モデル配置中... 🔒";
        if (progressBar) progressBar.style.width = "95%";
        if (percentText) percentText.textContent = "95%";

        // 成功完了
        if (progressBar) progressBar.style.width = "100%";
        if (percentText) percentText.textContent = "100%";
        if (statusText) statusText.textContent = "展開完了！🎉";

        showToast(res.message || "モデルZIPの安全展開・配置が完了しました！🎉", "success");
        setTimeout(() => {
          closeMyCoeUploadModal();
          loadMyCoeiroinkList(currentMyCoePage);
          if (typeof loadVoices === "function") loadVoices();
          if (typeof loadCharacterTerms === "function") loadCharacterTerms();
        }, 1200);
        return;
      }
    } catch (err) {
      console.error("[MYCOEIROINK Chunk Upload Error]", err);
      showToast(err.message, "error");
      if (statusText) statusText.textContent = `❌ エラー: ${err.message}`;
      if (progressBar) progressBar.style.background = "#ff4757";
      return;
    }
  }
}

// キャッシュ統計ダッシュボードサマリー集計のロード (D-2)
async function loadCacheStatsSummary() {
  try {
    const res = await fetch("/api/cache/stats/summary");
    if (!res.ok) return;
    const data = await res.json();
    if (!data.success) return;

    const dbSummary = data.db_summary || {};
    const l1 = data.l1_info || {};
    const disk = data.disk_info || {};

    // 1. 総ファイル数 & 容量
    const totalFiles = dbSummary.total_files || 0;
    const totalBytes = dbSummary.total_bytes || disk.total_disk_bytes || 0;
    const totalHits = dbSummary.total_hits || 0;

    const totalFilesEl = document.getElementById("cs-total-files");
    if (totalFilesEl) totalFilesEl.textContent = totalFiles.toLocaleString() + " 件";

    const totalBytesEl = document.getElementById("cs-total-bytes");
    if (totalBytesEl) totalBytesEl.textContent = `容量: ${(totalBytes / (1024 * 1024)).toFixed(1)} MB`;

    // 2. 累積ヒット数 & 効率バッジ
    const totalHitsEl = document.getElementById("cs-total-hits");
    if (totalHitsEl) totalHitsEl.textContent = totalHits.toLocaleString() + " 回";

    const effBadge = document.getElementById("cs-efficiency-badge");
    if (effBadge) {
      const totalAccess = totalFiles + totalHits;
      const rate = totalAccess > 0 ? ((totalHits / totalAccess) * 100).toFixed(1) : "0.0";
      effBadge.textContent = `ヒット効率: ${rate}%`;
    }

    // 3. L1 インメモリキャッシュ
    const l1ItemsEl = document.getElementById("cs-l1-items");
    if (l1ItemsEl) l1ItemsEl.textContent = `${l1.l1_items || 0} / ${l1.l1_max || 100} 件`;

    const l1BytesEl = document.getElementById("cs-l1-bytes");
    if (l1BytesEl) l1BytesEl.textContent = `メモリ: ${((l1.l1_bytes || 0) / 1024).toFixed(1)} KB`;

    // 4. 今月のTTSリクエスト
    const monthlyUsage = dbSummary.monthly_usage || {};
    let sumChars = 0;
    let sumReqs = 0;
    for (const p in monthlyUsage) {
      sumChars += monthlyUsage[p].characters || 0;
      sumReqs += monthlyUsage[p].requests || 0;
    }
    const monthlyReqsEl = document.getElementById("cs-monthly-reqs");
    if (monthlyReqsEl) monthlyReqsEl.textContent = sumReqs.toLocaleString() + " 回";
    const monthlyCharsEl = document.getElementById("cs-monthly-chars");
    if (monthlyCharsEl) monthlyCharsEl.textContent = `文字数: ${sumChars.toLocaleString()} 字`;

    // 5. プロバイダー別キャッシュ保有リスト
    const provsListEl = document.getElementById("cs-providers-list");
    if (provsListEl) {
      const providers = dbSummary.providers || {};
      if (Object.keys(providers).length === 0) {
        provsListEl.innerHTML = '<span style="color:var(--text-muted);">キャッシュデータなし</span>';
      } else {
        let html = '';
        for (const [p, stat] of Object.entries(providers)) {
          const mb = (stat.bytes / (1024 * 1024)).toFixed(1);
          html += `
            <div style="display:flex;justify-content:space-between;align-items:center;background:rgba(255,255,255,0.03);padding:4px 8px;border-radius:4px;">
              <span style="font-weight:600;text-transform:uppercase;color:var(--accent);">${p}</span>
              <span style="color:var(--text-dim);font-size:11px;">${stat.files.toLocaleString()} 件 (${mb} MB) / 🎯 ${stat.hits.toLocaleString()} hit</span>
            </div>
          `;
        }
        provsListEl.innerHTML = html;
      }
    }

    // 6. トップ単語リスト
    const topWordsListEl = document.getElementById("cs-top-words-list");
    if (topWordsListEl) {
      const topWords = dbSummary.top_words || [];
      if (topWords.length === 0) {
        topWordsListEl.innerHTML = '<span style="color:var(--text-muted);">キャッシュデータなし</span>';
      } else {
        let html = '';
        topWords.slice(0, 5).forEach((w, idx) => {
          const safeText = escapeHtml(w.text || "").slice(0, 20);
          html += `
            <div style="display:flex;justify-content:space-between;align-items:center;background:rgba(255,255,255,0.03);padding:4px 8px;border-radius:4px;">
              <span><strong style="color:var(--text-muted);font-size:10px;margin-right:4px;">#${idx + 1}</strong> "${safeText}"</span>
              <span class="badge" style="background:rgba(88,101,242,0.2);color:#5865f2;font-size:10px;">${w.hits.toLocaleString()} hits</span>
            </div>
          `;
        });
        topWordsListEl.innerHTML = html;
      }
    }
  } catch (err) {
    console.warn("Failed to load cache stats summary:", err);
  }
}
window.loadCacheStatsSummary = loadCacheStatsSummary;

// キャッシュ整合性スキャン（破損・無音ファイルの検出・自己修復）
async function scanCacheIntegrity() {
  const btn = document.getElementById("cache-scan-btn");
  const resultCard = document.getElementById("cache-scan-result-card");
  const titleEl = document.getElementById("cache-scan-title");
  const badgeEl = document.getElementById("cache-scan-badge");
  const detailsEl = document.getElementById("cache-scan-details");

  if (btn) {
    btn.disabled = true;
    btn.textContent = "スキャン検査中... ⏳";
  }
  if (resultCard) {
    resultCard.style.display = "block";
    if (titleEl) titleEl.textContent = "🔍 キャッシュ整合性スキャン実行中...";
    if (badgeEl) {
      badgeEl.textContent = "検査中";
      badgeEl.style.background = "#5865f2";
    }
    if (detailsEl) detailsEl.innerHTML = "WAVヘッダー構造、破損ビット、無音振幅（実効サンプルでの最小振幅検知）を順次スキャン中...";
  }

  try {
    const res = await fetch(`/api/cache/scan?guild_id=${currentGuildId}`, { method: "POST" });
    const data = await res.json();
    if (res.ok && data.success) {
      const r = data.result;
      const hasIssue = (r.corrupted > 0 || r.silent > 0);
      if (badgeEl) {
        badgeEl.textContent = hasIssue ? `修復完了 (${r.removed}件削除)` : "全件健全 ✅";
        badgeEl.style.background = hasIssue ? "#f0b232" : "#23a55a";
      }
      if (titleEl) titleEl.textContent = `🔍 キャッシュ整合性スキャン完了 (全 ${r.total_scanned} 件スキャン)`;

      let detailsHtml = `<strong>健全ファイル:</strong> ${r.healthy} 件 | <strong>破損:</strong> ${r.corrupted} 件 | <strong>無音:</strong> ${r.silent} 件 | <strong>自動削除修復:</strong> ${r.removed} 件`;
      if (r.details && r.details.length > 0) {
        detailsHtml += `<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.1);max-height:120px;overflow-y:auto;font-family:monospace;font-size:11px;">`;
        r.details.forEach(d => {
          detailsHtml += `<div>⚠️ ${d.filename}: ${d.reason}</div>`;
        });
        detailsHtml += `</div>`;
      }
      if (detailsEl) detailsEl.innerHTML = detailsHtml;
      showToast(`キャッシュスキャン完了: ${r.total_scanned}件検査 (健全: ${r.healthy}件, 修復削除: ${r.removed}件)`, "success");
      loadStats(currentGuildId);
    } else {
      if (badgeEl) {
        badgeEl.textContent = "エラー";
        badgeEl.style.background = "#ed4245";
      }
      if (detailsEl) detailsEl.textContent = data.error || "スキャンに失敗しました。";
      showToast(data.error || "スキャンに失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "キャッシュ健全性スキャン 🔍";
    }
  }
}

// --- ログビューア ---

async function loadLogs() {
  try {
    const sel = document.getElementById("log-type-select");
    const logType = sel ? sel.value : "bot";
    const res = await fetch(`/api/logs?type=${logType}&guild_id=${currentGuildId}`);
    const data = await res.json();
    const terminal = document.getElementById("log-terminal");
    if (terminal) {
      terminal.textContent = data.logs.join("");
      terminal.scrollTop = terminal.scrollHeight;
    }
  } catch (e) {
    console.error("Failed to load logs:", e);
  }
}

function toggleLogAutoRefresh(checkbox) {
  if (checkbox.checked) {
    logInterval = setInterval(loadLogs, 3000);
  } else {
    if (logInterval) clearInterval(logInterval);
  }
}

// --- 分散ワーカーノード リアルタイムログ ---
let currentWorkerLogTarget = "agent";
let workerLogInterval = null;
let _workerLogNodesSynced = false;

async function syncWorkerLogNodes() {
  if (_workerLogNodesSynced) return;
  const selNode = document.getElementById("worker-log-node-select");
  if (!selNode) return;
  try {
    const res = await fetch("/api/worker/logs?mode=list");
    const data = await res.json();
    if (data.success && Array.isArray(data.workers)) {
      _workerLogNodesSynced = true;
      const existing = new Set(Array.from(selNode.options).map(o => o.value));
      for (const w of data.workers) {
        if (w.node_id && !existing.has(w.node_id)) {
          const opt = document.createElement("option");
          opt.value = w.node_id;
          opt.textContent = w.label || w.hostname || w.node_id.replace(/^worker_/, "");
          selNode.appendChild(opt);
          existing.add(w.node_id);
        }
      }
    }
  } catch (e) {
    // 静的オプションがあるためフォールバック
  }
}

async function loadWorkerLogs(forceRefresh = false) {
  syncWorkerLogNodes();
  const selNode = document.getElementById("worker-log-node-select");
  const terminal = document.getElementById("worker-log-terminal");
  const badge = document.getElementById("worker-log-status-badge");
  if (!selNode || !terminal) return;

  const nodeId = selNode.value;
  if (!nodeId) return;

  if (badge) {
    badge.textContent = forceRefresh ? "同期中..." : "取得中...";
    badge.style.background = "#faa61a";
  }

  try {
    const url = `/api/worker/logs?node_id=${encodeURIComponent(nodeId)}&target=${encodeURIComponent(currentWorkerLogTarget)}&lines=150${forceRefresh ? '&refresh=1' : ''}`;
    const res = await fetch(url);
    const data = await res.json();

    if (data.success && Array.isArray(data.lines)) {
      if (data.lines.length === 0) {
        terminal.textContent = "(ログはまだありません)";
      } else {
        terminal.textContent = data.lines.join("\n");
        terminal.scrollTop = terminal.scrollHeight;
      }
      if (badge) {
        const timeStr = new Date().toLocaleTimeString();
        badge.textContent = `OK (${timeStr})`;
        badge.style.background = "#2ecc71";
      }
    } else {
      terminal.textContent = `エラー: ${data.error || "ログを取得できませんでした。"}`;
      if (badge) {
        badge.textContent = "取得エラー";
        badge.style.background = "#ed4245";
      }
    }
  } catch (err) {
    terminal.textContent = `通信エラー: ${err.message}`;
    if (badge) {
      badge.textContent = "通信エラー";
      badge.style.background = "#ed4245";
    }
  }
}

function switchWorkerLogTarget(target) {
  currentWorkerLogTarget = target;
  const targets = ["agent", "voicevox", "coeiroink", "sharevox"];
  for (const t of targets) {
    const btn = document.getElementById(`wlog-tab-${t}`);
    if (btn) {
      if (t === target) {
        btn.className = "btn btn-sm btn-primary";
      } else {
        btn.className = "btn btn-sm btn-secondary";
      }
    }
  }
  loadWorkerLogs(false);
}

function onWorkerLogNodeChanged() {
  loadWorkerLogs(false);
}

function toggleWorkerLogAutoRefresh(checkbox) {
  if (checkbox.checked) {
    loadWorkerLogs(false);
    workerLogInterval = setInterval(() => loadWorkerLogs(false), 5000);
  } else {
    if (workerLogInterval) clearInterval(workerLogInterval);
    workerLogInterval = null;
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/[&<>'"]/g, 
    tag => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[tag] || tag)
  );
}

// インラインHTML属性 (onclick等) から確実に呼び出せるようグローバルに公開
window.openDictBatchImportModal = openDictBatchImportModal;
window.closeDictBatchImportModal = closeDictBatchImportModal;
window.onDictBatchInputChanged = onDictBatchInputChanged;
window.executeDictBatchImport = executeDictBatchImport;
window.openUserImportModal = openUserImportModal;
window.closeUserImportModal = closeUserImportModal;
window.executeUserImport = executeUserImport;
window.onGuildChanged = onGuildChanged;
window.filterDictionary = filterDictionary;
window.addDictWord = addDictWord;
window.deleteDictWord = deleteDictWord;
window.saveVoiceSettings = saveVoiceSettings;
window.testPreviewAudio = testPreviewAudio;
window.saveBotConfig = saveBotConfig;
window.purgeCache = purgeCache;
window.loadLogs = loadLogs;
window.toggleLogAutoRefresh = toggleLogAutoRefresh;
window.loadWorkerLogs = loadWorkerLogs;
window.switchWorkerLogTarget = switchWorkerLogTarget;
window.onWorkerLogNodeChanged = onWorkerLogNodeChanged;
window.toggleWorkerLogAutoRefresh = toggleWorkerLogAutoRefresh;
window.loadGuildChannels = loadGuildChannels;
window.startEditDictWord = startEditDictWord;
window.saveEditDictWord = saveEditDictWord;
window.cancelEditDictWord = cancelEditDictWord;
window.switchCacheExplorerTarget = switchCacheExplorerTarget;
window.onCacheExplorerSearch = onCacheExplorerSearch;
window.prevCacheExplorerPage = prevCacheExplorerPage;
window.nextCacheExplorerPage = nextCacheExplorerPage;
window.loadCacheExplorerFiles = loadCacheExplorerFiles;
window.playCacheAudio = playCacheAudio;
window.deleteCacheFile = deleteCacheFile;
window.deleteSelectedCacheFiles = deleteSelectedCacheFiles;
window.toggleSelectAllCacheFiles = toggleSelectAllCacheFiles;
window.onCacheFileSelectChange = onCacheFileSelectChange;
window.checkModelsStatus = checkModelsStatus;
window.triggerDownloadSpeaker = triggerDownloadSpeaker;
window.triggerDownloadAllModels = triggerDownloadAllModels;

// --- ヘルプ・キャラクター個別規約テーブル ---
let CHARACTER_TERMS_DATA = [
  { name: "四国めたん", credit: "VOICEVOX:四国めたん", creator: "SSS合同会社", url: "https://zunko.jp/guideline.html" },
  { name: "ずんだもん", credit: "VOICEVOX:ずんだもん", creator: "SSS合同会社", url: "https://zunko.jp/guideline.html" },
  { name: "春日部つむぎ", credit: "VOICEVOX:春日部つむぎ", creator: "春日部つむぎ公式", url: "https://tsumugi-official.studio.site/rule" },
  { name: "雨晴はう", credit: "VOICEVOX:雨晴はう", creator: "雨晴はう公式", url: "https://ameharehau.wixsite.com/website/guideline" },
  { name: "波音リツ", credit: "VOICEVOX:波音リツ", creator: "Canon", url: "http://canon-voice.sakura.ne.jp/kiyaku.html" },
  { name: "玄野武宏", credit: "VOICEVOX:玄野武宏", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "白上虎太郎", credit: "VOICEVOX:白上虎太郎", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "青山龍星", credit: "VOICEVOX:青山龍星", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "冥鳴ひまり", credit: "VOICEVOX:冥鳴ひまり", creator: "冥鳴ひまり公式", url: "https://meimeihimari.wixsite.com/himari" },
  { name: "九州そら", credit: "VOICEVOX:九州そら", creator: "SSS合同会社", url: "https://zunko.jp/guideline.html" },
  { name: "もち子さん", credit: "VOICEVOX:もち子さん", creator: "明日葉友恵", url: "https://vturing.fanbox.cc/posts/2622748" },
  { name: "剣崎雌雄", credit: "VOICEVOX:剣崎雌雄", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "WhiteCUL", credit: "VOICEVOX:WhiteCUL", creator: "CUL PROJECT", url: "https://www.jcm-cul.jp/whitecul/" },
  { name: "後鬼", credit: "VOICEVOX:後鬼", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "No.7", credit: "VOICEVOX:No.7", creator: "SEVEN", url: "https://suzushiro-no7.tumblr.com/terms" },
  { name: "ちび式じい", credit: "VOICEVOX:ちび式じい", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "櫻歌ミコ", credit: "VOICEVOX:櫻歌ミコ", creator: "櫻歌ミコ公式", url: "https://miko35.info/terms/" },
  { name: "小夜/SAYO", credit: "VOICEVOX:小夜/SAYO", creator: "小夜公式", url: "https://315sayo.fanbox.cc/posts/3424103" },
  { name: "ナースロボ＿タイプＴ", credit: "VOICEVOX:ナースロボ＿タイプＴ", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "†聖騎士 紅桜†", credit: "VOICEVOX:†聖騎士 紅桜†", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "雀松朱司", credit: "VOICEVOX:雀松朱司", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "麒ヶ島宗麟", credit: "VOICEVOX:麒ヶ島宗麟", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "春歌ナナ", credit: "VOICEVOX:春歌ナナ", creator: "ななひら", url: "http://haruka-nana.sakura.ne.jp/" },
  { name: "猫使アル", credit: "VOICEVOX:猫使アル", creator: "猫使公式", url: "https://necousa-official.jimdofree.com/" },
  { name: "猫使ビィ", credit: "VOICEVOX:猫使ビィ", creator: "猫使公式", url: "https://necousa-official.jimdofree.com/" },
  { name: "中国うさぎ", credit: "VOICEVOX:中国うさぎ", creator: "SSS合同会社", url: "https://zunko.jp/guideline.html" },
  { name: "栗田まろん", credit: "VOICEVOX:栗田まろん", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "あいえるたん", credit: "VOICEVOX:あいえるたん", creator: "株式会社インフィニットループ", url: "https://www.infiniteloop.co.jp/special/iltan/" },
  { name: "満別花丸", credit: "VOICEVOX:満別花丸", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  { name: "琴詠ニア", credit: "VOICEVOX:琴詠ニア", creator: "VIRVOX PROJECT", url: "https://virvoxproject.wixsite.com/official/terms" },
  // --- SHAREVOX ---
  { name: "小春音アミ (SHAREVOX)", credit: "SHAREVOX:小春音アミ", creator: "小春音アミ公式", url: "https://sharevox.app/characters/ami" },
  { name: "つくよみちゃん (SHAREVOX)", credit: "SHAREVOX:つくよみちゃん", creator: "夢前黎", url: "https://tyc.rei-yumesaki.net/material/rules/" },
  { name: "らごぱすブラック (SHAREVOX)", credit: "SHAREVOX:らごぱすブラック", creator: "SHAREVOX Project", url: "https://sharevox.app/terms" },
  { name: "らごぱすホワイト (SHAREVOX)", credit: "SHAREVOX:らごぱすホワイト", creator: "SHAREVOX Project", url: "https://sharevox.app/terms" },
  { name: "白痴ー/黒聡鵜月 (SHAREVOX)", credit: "SHAREVOX:白痴ー/黒聡鵜月", creator: "SHAREVOX Project", url: "https://sharevox.app/terms" },
  { name: "Yくん/開発者 (SHAREVOX)", credit: "SHAREVOX:Yくん/開発者", creator: "SHAREVOX Project", url: "https://sharevox.app/terms" },
  { name: "風花ゆき (SHAREVOX)", credit: "SHAREVOX:風花ゆき", creator: "SHAREVOX Project", url: "https://sharevox.app/terms" },
  { name: "安倍広葉 (SHAREVOX)", credit: "SHAREVOX:安倍広葉", creator: "SHAREVOX Project", url: "https://sharevox.app/terms" },
  { name: "鈴乃 (SHAREVOX)", credit: "SHAREVOX:鈴乃", creator: "SHAREVOX Project", url: "https://sharevox.app/terms" },
  // --- COEIROINK ---
  { name: "つくよみちゃん (COEIROINK)", credit: "COEIROINK:つくよみちゃん", creator: "夢前黎", url: "https://tyc.rei-yumesaki.net/material/rules/" },
  { name: "MANA (COEIROINK)", credit: "COEIROINK:MANA", creator: "鹿仲茉菜", url: "https://coeiroink.com/character/official/mana" },
  { name: "KANA (COEIROINK)", credit: "COEIROINK:KANA", creator: "眞中佳奈", url: "https://coeiroink.com/character/official/kana" },
  { name: "おふとんP (COEIROINK)", credit: "COEIROINK:おふとんP", creator: "おふとんP", url: "https://coeiroink.com/character/official/ofuton-p" },
  { name: "アメノちゃん (COEIROINK)", credit: "COEIROINK:アメノちゃん", creator: "アメノちゃん公式", url: "https://coeiroink.com/character/official/ameno" },
  { name: "ディアちゃん (COEIROINK)", credit: "COEIROINK:ディアちゃん", creator: "ディアちゃん公式", url: "https://coeiroink.com/character/official/dia" },
  { name: "アルマちゃん (COEIROINK)", credit: "COEIROINK:アルマちゃん", creator: "アルマちゃん公式", url: "https://coeiroink.com/character/official/arma" },
  { name: "リリンちゃん (COEIROINK)", credit: "COEIROINK:リリンちゃん", creator: "リリンちゃん公式", url: "https://coeiroink.com/character/official/lilin" },
  { name: "クロワちゃん (COEIROINK)", credit: "COEIROINK:クロワちゃん", creator: "クロワちゃん公式", url: "https://coeiroink.com/character/official/croix" },
  { name: "アンジーさん (COEIROINK)", credit: "COEIROINK:アンジーさん", creator: "アンジーさん公式", url: "https://coeiroink.com/character/official/andzie" },
  { name: "AI声優-朱花 (COEIROINK)", credit: "COEIROINK:AI声優-朱花", creator: "AI声優公式", url: "https://coeiroink.com/character/official/ayaka" },
  { name: "AI声優-青葉 (COEIROINK)", credit: "COEIROINK:AI声優-青葉", creator: "AI声優公式", url: "https://coeiroink.com/character/official/aoba" },
  { name: "AI声優-銀芽 (COEIROINK)", credit: "COEIROINK:AI声優-銀芽", creator: "AI声優公式", url: "https://coeiroink.com/character/official/ginga" },
  { name: "AI声優-金苗 (COEIROINK)", credit: "COEIROINK:AI声優-金苗", creator: "AI声優公式", url: "https://coeiroink.com/character/official/kanae" },
  // --- MYCOEIROINK ---
  { name: "あみたろ (MYCOEIROINK)", credit: "COEIROINK:あみたろ", creator: "あみたろの声素材工房", url: "https://www14.big.or.jp/~amiami/happy/" },
  { name: "松嘩りすく (MYCOEIROINK)", credit: "COEIROINK:松嘩りすく", creator: "りすく工房", url: "https://lit.link/riskmatuka" },
  // --- OpenJTalk ---
  { name: "メイ (OpenJTalk)", credit: "Open JTalk:メイ", creator: "名古屋工業大学 MMDAgent", url: "https://www.mmdagent.jp/" },
  { name: "たくみ (OpenJTalk)", credit: "Open JTalk:たくみ", creator: "名古屋工業大学 MMDAgent", url: "https://www.mmdagent.jp/" },
  { name: "名工大 男声 (OpenJTalk)", credit: "Open JTalk:nitech_jp_atr503_m001", creator: "名古屋工業大学 / ATR", url: "http://open-jtalk.sourceforge.net/" },
  { name: "ゆっくり風 (OpenJTalk)", credit: "Open JTalk (機械調)", creator: "名古屋工業大学 MMDAgent", url: "http://open-jtalk.sourceforge.net/" }
];

async function loadCharacterTerms() {
  try {
    const res = await fetch("/api/character_terms");
    if (res.ok) {
      const data = await res.json();
      if (data && Array.isArray(data.terms)) {
        CHARACTER_TERMS_DATA = data.terms;
        renderCharacterTermsTable();
      }
    }
  } catch (e) {
    console.debug("Failed to load dynamic character terms:", e);
  }
}

let termsFilterQuery = "";

function onTermsSearchInput(query) {
  termsFilterQuery = (query || "").trim().toLowerCase();
  renderCharacterTermsTable();
}

function renderCharacterTermsTable() {
  const tbody = document.getElementById("character-terms-tbody");
  if (!tbody) return;

  const filtered = CHARACTER_TERMS_DATA.filter(c => {
    if (!termsFilterQuery) return true;
    return c.name.toLowerCase().includes(termsFilterQuery) ||
           c.creator.toLowerCase().includes(termsFilterQuery) ||
           c.credit.toLowerCase().includes(termsFilterQuery);
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="4" style="text-align:center;padding:24px;color:var(--text-dim);">
          🔍 「${escapeHtml(termsFilterQuery)}」に一致するキャラクターは見つかりませんでした。
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = filtered.map(c => `
    <tr style="border-bottom:1px solid var(--border-color);transition:background 0.2s;" onmouseover="this.style.background='var(--bg-card-hover)'" onmouseout="this.style.background='transparent'">
      <td style="padding:10px 12px;font-weight:600;color:var(--text-main);">${escapeHtml(c.name)}</td>
      <td style="padding:10px 12px;"><code style="background:var(--bg-body);padding:3px 6px;border-radius:4px;font-size:12px;color:#23a55a;">${escapeHtml(c.credit)}</code></td>
      <td style="padding:10px 12px;color:var(--text-muted);">${escapeHtml(c.creator)}</td>
      <td style="padding:10px 12px;text-align:center;">
        <a href="${c.url}" target="_blank" rel="noopener" class="btn btn-secondary btn-sm" style="padding:4px 10px;font-size:11px;text-decoration:none;display:inline-flex;align-items:center;gap:4px;">
          公式規約 ↗
        </a>
      </td>
    </tr>
  `).join("");
}

window.onTermsSearchInput = onTermsSearchInput;
window.renderCharacterTermsTable = renderCharacterTermsTable;
window.openDictBatchImportModal = openDictBatchImportModal;
window.closeDictBatchImportModal = closeDictBatchImportModal;
window.onDictBatchInputChanged = onDictBatchInputChanged;
window.executeDictBatchImport = executeDictBatchImport;
window.onDictConflictChange = onDictConflictChange;
window.setAllDictConflicts = setAllDictConflicts;
window.loadMyCoeiroinkList = loadMyCoeiroinkList;
window.syncMyCoeiroink = syncMyCoeiroink;
window.onMyCoeSearchInput = onMyCoeSearchInput;
window.onMyCoeFilterChange = onMyCoeFilterChange;
window.changeMyCoePage = changeMyCoePage;
window.playMyCoeSample = playMyCoeSample;
window.copyMyCoeUuid = copyMyCoeUuid;
window.triggerDisasterTest = triggerDisasterTest;
window.updateDisasterVolumeVal = updateDisasterVolumeVal;
window.setDisasterVolumePreset = setDisasterVolumePreset;
window.filterVoiceGender = filterVoiceGender;
window.selectVoiceDetail = selectVoiceDetail;
window.renderVoiceChips = renderVoiceChips;
window.updateVolumeVal = updateVolumeVal;
window.updateSpeedVal = updateSpeedVal;
window.updatePitchVal = updatePitchVal;
window.tryAutoInstallMyCoe = tryAutoInstallMyCoe;
window.openMyCoeUploadModal = openMyCoeUploadModal;
window.closeMyCoeUploadModal = closeMyCoeUploadModal;
window.onMyCoeDragOver = onMyCoeDragOver;
window.onMyCoeDragLeave = onMyCoeDragLeave;
window.onMyCoeDrop = onMyCoeDrop;
window.onMyCoeFileSelected = onMyCoeFileSelected;
window.resetPitchSpeedDefault = resetPitchSpeedDefault;
window.loadCharacterTerms = loadCharacterTerms;

// --- 管理者専用: メンバー音声設定管理 ---

var MEMBER_VOICES_DATA = [];
var memberVoiceFilterQuery = "";
var memberEditAudio = new Audio();

async function loadMemberVoices(guildId = null) {
  const targetGid = guildId || currentGuildId;
  if (!targetGid) return;

  const tbody = document.getElementById("member-voices-table-body");
  if (tbody) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:32px;color:var(--text-muted);"><div class="loading-spinner"></div> メンバーの音声設定を読み込み中...</td></tr>`;
  }

  let users = null;
  try {
    const res = await fetch(`/api/admin/guild_users?guild_id=${targetGid}`);
    if (res.status === 403) {
      currentGuildPerms.has_role = false;
      currentGuildPerms.is_admin = false;
      updatePermissionsUI();
      return;
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.error || "メンバー設定の取得に失敗しました", "error");
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:24px;color:#ff4757;">⚠️ 取得エラー: ${escapeHtml(err.error || "読み込み失敗")}</td></tr>`;
      }
      return;
    }
    const data = await res.json();
    users = Array.isArray(data.users) ? data.users : [];
  } catch (e) {
    console.error("Failed to fetch member voices from API:", e);
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:24px;color:#ff4757;">⚠️ サーバーとの通信に失敗しました。再読み込みをお試しください。</td></tr>`;
    }
    return;
  }

  try {
    MEMBER_VOICES_DATA = users;
    renderMemberVoicesTable();
  } catch (e) {
    console.error("Failed to render member voices table:", e);
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:24px;color:#ff4757;">⚠️ 表示処理中にエラーが発生しました: ${escapeHtml(e.message || String(e))}</td></tr>`;
    }
  }
}

function onMemberVoiceSearchInput(query) {
  memberVoiceFilterQuery = (query || "").trim().toLowerCase();
  renderMemberVoicesTable();
}

function renderMemberVoicesTable() {
  const tbody = document.getElementById("member-voices-table-body");
  const countBadge = document.getElementById("member-voices-count-badge");
  if (!tbody) return;

  const totalCount = MEMBER_VOICES_DATA.length;
  if (countBadge) {
    countBadge.textContent = `👥 設定済みメンバー: ${totalCount}名`;
  }

  if (totalCount === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="6" style="text-align:center;padding:48px 16px;color:var(--text-muted);">
          <div style="font-size:32px;margin-bottom:8px;">👥</div>
          <div style="font-weight:600;font-size:15px;color:var(--text-light);margin-bottom:4px;">個人設定を持つメンバーはいません</div>
          <div style="font-size:13px;">このサーバーでBotを利用して発言したメンバーや、WebUIで個人設定を保存したメンバーの設定がここに表示されます。</div>
        </td>
      </tr>
    `;
    return;
  }

  const filtered = MEMBER_VOICES_DATA.filter(u => {
    if (!u) return false;
    if (!memberVoiceFilterQuery) return true;
    const q = memberVoiceFilterQuery;
    const nameMatch = (u.display_name || "").toLowerCase().includes(q) || (u.username || "").toLowerCase().includes(q);
    const idMatch = (u.user_id || "").includes(q);
    const v = u.voice || {};
    const voiceMatch = (v.speaker_name || "").toLowerCase().includes(q) || (v.provider || "").toLowerCase().includes(q);
    return nameMatch || idMatch || voiceMatch;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="6" style="text-align:center;padding:36px;color:var(--text-muted);">
          「${escapeHtml(memberVoiceFilterQuery)}」に一致するメンバーは見つかりませんでした。
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = filtered.map(u => {
    const v = u.voice || {};
    const provBadgeColor = getProviderBadgeColor(v.provider || "UNKNOWN");
    const speedText = `${Number(v.speed !== undefined ? v.speed : 1.0).toFixed(2)}x`;
    const pitchText = v.pitch || "0%";
    const volumePercent = Math.round((v.volume !== undefined ? v.volume : 0.8) * 100);
    const inGuildBadge = u.is_in_guild 
      ? '<span class="badge" style="background:rgba(46,204,113,0.15);color:#2ecc71;font-size:10px;padding:2px 6px;">サーバー在室</span>'
      : '<span class="badge" style="background:rgba(149,165,166,0.15);color:#95a5a6;font-size:10px;padding:2px 6px;">退室済み</span>';

    const safeUid = String(u.user_id || "").replace(/[^0-9]/g, "");
    const avatarHtml = u.avatar_url
      ? `
        <div style="position:relative;width:38px;height:38px;flex-shrink:0;">
          <img src="${escapeHtml(u.avatar_url)}" alt="Avatar" loading="lazy" style="width:38px;height:38px;border-radius:50%;border:1px solid rgba(255,255,255,0.18);object-fit:cover;display:block;" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';" />
          <div style="display:none;width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.12);align-items:center;justify-content:center;font-size:18px;">👤</div>
        </div>
      `
      : `
        <div style="width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.12);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;">👤</div>
      `;

    return `
      <tr style="border-bottom:1px solid rgba(255,255,255,0.05);transition:background 0.2s;" onmouseover="this.style.background='rgba(255,255,255,0.02)'" onmouseout="this.style.background='transparent'">
        <td style="padding:12px 14px;">
          <div style="display:flex;align-items:center;gap:12px;">
            ${avatarHtml}
            <div style="overflow:hidden;">
              <div style="display:flex;align-items:center;gap:6px;">
                <span style="font-weight:700;font-size:14px;color:var(--text-light);">${escapeHtml(u.display_name || u.username || u.user_id)}</span>
                ${inGuildBadge}
              </div>
              <div style="font-size:11px;color:var(--text-muted);font-family:monospace;">
                @${escapeHtml(u.username || u.user_id)} <span style="opacity:0.6;">(${escapeHtml(u.user_id)})</span>
              </div>
            </div>
          </div>
        </td>
        <td style="padding:12px 14px;">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
            <span class="badge" style="background:${provBadgeColor};color:white;font-size:10px;padding:2px 6px;">
              ${escapeHtml(v.provider || "UNKNOWN")}
            </span>
            <span style="font-weight:600;font-size:13px;color:var(--text-light);">
              ${escapeHtml(v.speaker_name || v.speaker_id || "デフォルト")}
            </span>
          </div>
        </td>
        <td style="padding:12px 14px;">
          <span class="badge" style="background:rgba(88,101,242,0.15);color:#7289da;font-size:12px;font-weight:600;padding:3px 8px;">
            ${speedText}
          </span>
        </td>
        <td style="padding:12px 14px;">
          <span class="badge" style="background:rgba(235,69,158,0.15);color:#eb459e;font-size:12px;font-weight:600;padding:3px 8px;">
            ${escapeHtml(pitchText)}
          </span>
        </td>
        <td style="padding:12px 14px;">
          <span class="badge" style="background:rgba(241,196,15,0.15);color:#f1c40f;font-size:12px;font-weight:600;padding:3px 8px;">
            ${volumePercent}%
          </span>
        </td>
        <td style="padding:12px 14px;text-align:right;white-space:nowrap;">
          <button type="button" class="btn btn-secondary btn-sm" onclick="previewMemberVoiceDirect('${safeUid}')" title="この設定で試聴" style="padding:5px 9px;margin-right:4px;">
            🔊
          </button>
          <button type="button" class="btn btn-primary btn-sm" onclick="openMemberVoiceEditModal('${safeUid}')" title="設定を編集" style="padding:5px 11px;margin-right:4px;">
            ✏️ 編集
          </button>
          <button type="button" class="btn btn-danger btn-sm" onclick="confirmResetMemberVoice('${safeUid}')" title="設定をリセット" style="background:#ed4245;color:white;border:none;padding:5px 9px;">
            🗑️
          </button>
        </td>
      </tr>
    `;
  }).join("");
}

function getProviderBadgeColor(provider) {
  switch ((provider || "").toUpperCase()) {
    case "VOICEVOX": return "#008080";
    case "SHAREVOX": return "#2d8a4e";
    case "COEIROINK": return "#1a73e8";
    case "OPENJTALK": return "#8e44ad";
    case "AZURE": return "#0078d4";
    case "GOOGLE": return "#ea4335";
    case "EDGE": return "#008272";
    default: return "#5865f2";
  }
}

async function previewMemberVoiceDirect(userId) {
  const user = MEMBER_VOICES_DATA.find(u => u.user_id === userId);
  if (!user) return;
  const v = user.voice;
  try {
    const text = "こんにちは！超低遅延で自然な音声をお届けします。";
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        speaker_id: v.speaker_id,
        text: text,
        pitch: v.pitch || "0%",
        speed: v.speed || 1.0,
        volume: v.volume || 0.8
      })
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.error || "試聴の生成に失敗しました", "error");
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    memberEditAudio.src = url;
    memberEditAudio.volume = Math.min(1.0, (v.volume || 0.8));
    await memberEditAudio.play();
  } catch (e) {
    showToast("試聴再生中にエラーが発生しました", "error");
  }
}

function openMemberVoiceEditModal(userId) {
  const user = MEMBER_VOICES_DATA.find(u => u.user_id === userId);
  if (!user) return;

  const avatarImgEl = document.getElementById("member-edit-avatar-img");
  const avatarFallbackEl = document.getElementById("member-edit-avatar-fallback");
  const nameEl = document.getElementById("member-edit-name");
  const idEl = document.getElementById("member-edit-id");
  const userIdInput = document.getElementById("member-edit-user-id");
  const speakerSelect = document.getElementById("member-edit-speaker");
  const bracketsCheck = document.getElementById("member-edit-brackets");

  if (avatarImgEl && avatarFallbackEl) {
    if (user.avatar_url) {
      avatarImgEl.src = user.avatar_url;
      avatarImgEl.style.display = "block";
      avatarFallbackEl.style.display = "none";
    } else {
      avatarImgEl.src = "";
      avatarImgEl.style.display = "none";
      avatarFallbackEl.style.display = "flex";
    }
  }
  nameEl.textContent = `${user.display_name} (@${user.username})`;
  idEl.textContent = `ID: ${user.user_id}`;
  userIdInput.value = user.user_id;

  // 話者ドロップダウン生成
  speakerSelect.innerHTML = "";
  const grouped = {};
  availableVoices.forEach(spk => {
    const prov = spk.provider || "OTHER";
    if (!grouped[prov]) grouped[prov] = [];
    grouped[prov].push(spk);
  });

  Object.keys(grouped).forEach(prov => {
    const optgroup = document.createElement("optgroup");
    optgroup.label = `--- ${prov} ---`;
    grouped[prov].forEach(spk => {
      const opt = document.createElement("option");
      opt.value = spk.id;
      opt.textContent = `${spk.name} (${spk.id})`;
      optgroup.appendChild(opt);
    });
    speakerSelect.appendChild(optgroup);
  });

  speakerSelect.value = user.voice.speaker_id;

  // 速度
  const speed = user.voice.speed !== undefined ? user.voice.speed : 1.0;
  updateMemberEditSpeed(speed);

  // ピッチ
  let pitchVal = 0;
  if (user.voice.pitch) {
    const m = user.voice.pitch.match(/([+-]?\d+)/);
    if (m) pitchVal = parseInt(m[1], 10);
  }
  updateMemberEditPitch(pitchVal);

  // 音量
  const vol = user.voice.volume !== undefined ? user.voice.volume : 0.8;
  const volPercent = Math.round(vol * 100);
  updateMemberEditVolume(volPercent);

  // 括弧
  bracketsCheck.checked = user.voice.strip_name_brackets !== false;

  modal.style.display = "flex";
}

function closeMemberVoiceModal() {
  const modal = document.getElementById("member-voice-edit-modal");
  if (modal) modal.style.display = "none";
  if (memberEditAudio) memberEditAudio.pause();
}

function updateMemberEditSpeed(val) {
  const num = parseFloat(val);
  const input = document.getElementById("member-edit-speed");
  const badge = document.getElementById("member-edit-speed-val");
  if (input) input.value = num;
  if (badge) badge.textContent = `${num.toFixed(2)}x`;
}

function setMemberEditSpeed(val) {
  updateMemberEditSpeed(val);
}

function updateMemberEditPitch(val) {
  const num = parseInt(val, 10);
  const input = document.getElementById("member-edit-pitch");
  const badge = document.getElementById("member-edit-pitch-val");
  if (input) input.value = num;
  const sign = num > 0 ? "+" : "";
  if (badge) badge.textContent = `${sign}${num}%`;
}

function setMemberEditPitch(val) {
  updateMemberEditPitch(val);
}

function updateMemberEditVolume(val) {
  const num = parseInt(val, 10);
  const input = document.getElementById("member-edit-volume");
  const badge = document.getElementById("member-edit-volume-val");
  if (input) input.value = num;
  if (badge) badge.textContent = `${num}%`;
}

function setMemberEditVolume(val) {
  updateMemberEditVolume(val);
}

async function previewMemberVoiceInModal() {
  const speakerId = document.getElementById("member-edit-speaker").value;
  const speed = parseFloat(document.getElementById("member-edit-speed").value);
  const pitchNum = parseInt(document.getElementById("member-edit-pitch").value, 10);
  const pitch = `${pitchNum > 0 ? "+" : ""}${pitchNum}%`;
  const volumePercent = parseInt(document.getElementById("member-edit-volume").value, 10);
  const volume = volumePercent / 100.0;

  const btn = document.getElementById("member-edit-preview-btn");
  if (btn) btn.disabled = true;

  try {
    const text = "こんにちは！超低遅延で自然な音声をお届けします。";
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        speaker_id: speakerId,
        text: text,
        pitch: pitch,
        speed: speed,
        volume: volume
      })
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.error || "試聴の生成に失敗しました", "error");
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    memberEditAudio.src = url;
    memberEditAudio.volume = Math.min(1.0, volume);
    await memberEditAudio.play();
  } catch (e) {
    showToast("試聴再生中にエラーが発生しました", "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function saveMemberVoiceFromModal() {
  const userId = document.getElementById("member-edit-user-id").value;
  if (!userId) return;

  const speakerId = document.getElementById("member-edit-speaker").value;
  const speed = parseFloat(document.getElementById("member-edit-speed").value);
  const pitchNum = parseInt(document.getElementById("member-edit-pitch").value, 10);
  const pitch = `${pitchNum > 0 ? "+" : ""}${pitchNum}%`;
  const volumePercent = parseInt(document.getElementById("member-edit-volume").value, 10);
  const volume = volumePercent / 100.0;
  const stripBrackets = document.getElementById("member-edit-brackets").checked;

  const saveBtn = document.getElementById("member-edit-save-btn");
  if (saveBtn) saveBtn.disabled = true;

  try {
    const res = await fetch(`/api/admin/guild_users/${userId}?guild_id=${currentGuildId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        speaker_id: speakerId,
        pitch: pitch,
        speed: speed,
        volume: volume,
        strip_name_brackets: stripBrackets
      })
    });

    if (res.ok) {
      showToast("メンバーの音声設定を保存しました！", "success");
      closeMemberVoiceModal();
      await loadMemberVoices(currentGuildId);
    } else {
      const err = await res.json().catch(() => ({}));
      showToast(err.error || "保存に失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function confirmResetMemberVoice(userId) {
  const user = MEMBER_VOICES_DATA.find(u => u && u.user_id === userId);
  const displayName = user ? (user.display_name || user.username || userId) : userId;
  if (!confirm(`「${displayName}」の個人設定をリセットしますか？\nリセットするとサーバーデフォルトまたは初期選定状態に戻ります。`)) {
    return;
  }
  await executeResetMemberVoice(userId);
}

async function deleteMemberVoiceFromModal() {
  const userId = document.getElementById("member-edit-user-id").value;
  if (!userId) return;
  if (!confirm("このメンバーの個人設定を削除（リセット）しますか？")) return;
  closeMemberVoiceModal();
  await executeResetMemberVoice(userId);
}

async function executeResetMemberVoice(userId) {
  try {
    const res = await fetch(`/api/admin/guild_users/${userId}?guild_id=${currentGuildId}`, {
      method: "DELETE"
    });
    if (res.ok) {
      showToast("個人設定をリセットしました", "success");
      await loadMemberVoices(currentGuildId);
    } else {
      const err = await res.json().catch(() => ({}));
      showToast(err.error || "リセットに失敗しました", "error");
    }
  } catch (e) {
    showToast("通信エラーが発生しました", "error");
  }
}

window.loadMemberVoices = loadMemberVoices;
window.onMemberVoiceSearchInput = onMemberVoiceSearchInput;
window.renderMemberVoicesTable = renderMemberVoicesTable;
window.openMemberVoiceEditModal = openMemberVoiceEditModal;
window.closeMemberVoiceModal = closeMemberVoiceModal;
window.updateMemberEditSpeed = updateMemberEditSpeed;
window.setMemberEditSpeed = setMemberEditSpeed;
window.updateMemberEditPitch = updateMemberEditPitch;
window.setMemberEditPitch = setMemberEditPitch;
window.updateMemberEditVolume = updateMemberEditVolume;
window.setMemberEditVolume = setMemberEditVolume;
window.previewMemberVoiceInModal = previewMemberVoiceInModal;
window.previewMemberVoiceDirect = previewMemberVoiceDirect;
window.saveMemberVoiceFromModal = saveMemberVoiceFromModal;
window.confirmResetMemberVoice = confirmResetMemberVoice;
window.deleteMemberVoiceFromModal = deleteMemberVoiceFromModal;

// --- 🚨 遅延・アンダーラン インシデント履歴 (原因調査用・DB永続化) ---

async function loadLatencyIncidents() {
  const tbody = document.getElementById("latency-incidents-tbody");
  const countBadge = document.getElementById("latency-incidents-count");
  if (!tbody) return;

  tbody.innerHTML = '<tr><td colspan="6" style="padding:16px;text-align:center;color:var(--text-muted);"><div class="loading-spinner"></div> インシデント履歴を読み込み中...</td></tr>';

  try {
    const res = await fetch("/api/system/latency/incidents?limit=50");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const incidents = Array.isArray(data.incidents) ? data.incidents : [];

    if (countBadge) {
      countBadge.textContent = `${incidents.length}件`;
    }

    if (incidents.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="padding:16px;text-align:center;color:#2ecc71;">✨ 記録された遅延・音途切れインシデントはありません（全発話が健全に処理されています）</td></tr>';
      return;
    }

    tbody.innerHTML = incidents.map(inc => {
      const occ = inc.occurred_at ? inc.occurred_at.replace("T", " ").slice(5, 19) : "--";
      const rec = inc.record || {};
      const user = rec.user_name || "不明";
      const text = inc.full_text || rec.text || rec.text_preview || "";
      const ttfs = rec.turn_ttfs_ms !== undefined ? Math.round(rec.turn_ttfs_ms) : "--";
      const underrun = rec.underrun_ms !== undefined ? Math.round(rec.underrun_ms) : 0;

      // 種別バッジ
      const types = inc.incident_types || [];
      const typeBadges = types.map(t => {
        let label = t;
        let bg = "rgba(240,71,71,0.2)";
        let color = "#ff9b9b";
        if (t === "A_UNDERRUN") { label = "⚠️ 音途切れ"; bg = "rgba(240,71,71,0.3)"; color = "#ff6b6b"; }
        else if (t === "B_PREROLL_TIMEOUT") { label = "⏳ プリロール上限"; bg = "rgba(250,166,26,0.2)"; color = "#f1c40f"; }
        else if (t === "C_SLOW_FIRST_AUDIO") { label = "🐢 初動遅延"; bg = "rgba(250,166,26,0.2)"; color = "#f1c40f"; }
        else if (t === "D_RTF_OVER") { label = "📈 RTF超過"; bg = "rgba(235,69,158,0.2)"; color = "#eb459e"; }
        else if (t === "E_FALLBACK") { label = "☁️ クラウド切替"; bg = "rgba(88,101,242,0.2)"; color = "#7289da"; }
        else if (t === "F_QUEUE_WAIT") { label = "👥 キュー滞留"; bg = "rgba(149,165,166,0.2)"; color = "#95a5a6"; }
        return `<span class="badge" style="background:${bg};color:${color};font-size:10px;padding:2px 5px;margin-right:3px;">${escapeHtml(label)}</span>`;
      }).join("");

      // 原因バッジ
      const causes = inc.root_causes || [];
      const causeBadges = causes.map(c => {
        let label = c;
        let bg = "rgba(255,255,255,0.06)";
        let color = "var(--text-light)";
        if (c === "SPECULATIVE_FALLBACK") { label = "⚡ 投機フォールバック (ローカル遅延)"; color = "#50e6ff"; bg = "rgba(0,176,244,0.15)"; }
        else if (c === "QUEUE_CONGESTION") { label = "⏳ 連投順番待ち (正常)"; color = "var(--text-muted)"; }
        else if (c.startsWith("UNDERRUN_CHUNK_")) { label = `⚠️ 途切れ (#${c.split('_').pop()})`; color = "#ff6b6b"; }
        else if (c.startsWith("WORKER_SLOW_CHUNK_")) { label = `🐢 処理遅延 (#${c.split('_').pop()})`; color = "#f1c40f"; }
        else if (c.startsWith("COEIROINK_COLD_LOAD_")) { label = `❄️ モデル初読込 (#${c.split('_').pop()})`; color = "#00b0f4"; }
        return `<span class="badge" style="background:${bg};color:${color};font-size:10px;padding:2px 5px;margin-right:3px;">${escapeHtml(label)}</span>`;
      }).join("");

      const chunks = rec.chunks || [];
      let chunksDetailHtml = "";
      if (chunks.length > 0) {
        chunksDetailHtml = `
          <div style="margin-top:6px;padding:6px 8px;background:rgba(0,0,0,0.3);border-radius:4px;font-size:10px;">
            <div style="font-weight:600;color:var(--text-muted);margin-bottom:4px;">チャンク詳細:</div>
            ${chunks.map(ch => `
              <div style="display:flex;gap:8px;align-items:center;padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.03);">
                <span style="color:var(--text-dim);width:24px;">#${ch.chunk_index}</span>
                <span style="color:var(--text-light);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">"${escapeHtml(ch.text)}"</span>
                <span style="color:${ch.provider === 'edge' || ch.provider === 'azure' ? '#50e6ff' : '#a8ff78'};">${escapeHtml(ch.worker || ch.provider)}</span>
                <span style="font-family:monospace;color:var(--text-muted);">${ch.is_cached ? '⚡キャッシュ' : Math.round(ch.synth_ms || 0) + 'ms'}</span>
              </div>
            `).join("")}
          </div>
        `;
      }

      return `
        <tr style="border-bottom:1px solid rgba(255,255,255,0.04);transition:background 0.2s;" onmouseover="this.style.background='rgba(255,255,255,0.02)'" onmouseout="this.style.background='transparent'">
          <td style="padding:8px 8px;font-family:monospace;color:var(--text-muted);white-space:nowrap;">${occ}</td>
          <td style="padding:8px 8px;max-width:260px;">
            <div style="font-weight:600;color:var(--text-light);font-size:11px;">${escapeHtml(user)}</div>
            <div style="color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;" title="${escapeHtml(text)}">${escapeHtml(text)}</div>
            ${chunksDetailHtml}
          </td>
          <td style="padding:8px 8px;">${typeBadges || '--'}</td>
          <td style="padding:8px 8px;">${causeBadges || '--'}</td>
          <td style="padding:8px 8px;text-align:right;font-family:monospace;font-weight:600;color:${ttfs > 1500 ? '#f1c40f' : 'var(--text-light)'};">${ttfs} ms</td>
          <td style="padding:8px 8px;text-align:right;font-family:monospace;font-weight:600;color:${underrun > 0 ? '#ff6b6b' : 'var(--text-dim)'};">${underrun > 0 ? underrun + ' ms' : '0 ms'}</td>
        </tr>
      `;
    }).join("");
  } catch (e) {
    console.error("loadLatencyIncidents error:", e);
    tbody.innerHTML = `<tr><td colspan="6" style="padding:16px;text-align:center;color:#ff6b6b;">⚠️ インシデント履歴の取得に失敗しました: ${escapeHtml(e.message)}</td></tr>`;
  }
}

window.loadLatencyIncidents = loadLatencyIncidents;




// ==========================================
// 1. 管理者パスワードログイン処理
// ==========================================
async function handlePasswordLogin(event) {
  event.preventDefault();
  const pwdInput = document.getElementById("admin-login-pwd");
  const errDiv = document.getElementById("admin-login-err");
  const btn = document.getElementById("admin-login-btn");
  if (!pwdInput) return;

  const password = pwdInput.value.trim();
  if (!password) return;

  if (btn) btn.disabled = true;
  if (errDiv) errDiv.style.display = "none";

  try {
    const res = await fetch("/api/auth/login_password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password })
    });
    const data = await res.json();
    if (res.ok && (data.success || data.status === "ok")) {
      showToast("管理者としてログインしました 🔑", "success");
      setTimeout(() => {
        window.location.reload();
      }, 500);
    } else {
      if (errDiv) {
        errDiv.textContent = `❌ ${data.error || "パスワードが正しくありません"}`;
        errDiv.style.display = "block";
      }
      showToast(data.error || "ログインに失敗しました", "error");
    }
  } catch (err) {
    if (errDiv) {
      errDiv.textContent = `❌ 通信エラー: ${err.message}`;
      errDiv.style.display = "block";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}
// ==========================================
// 1.5. Manager Node (HA Active-Standby) 管理 & Worker ノード統制
// ==========================================
function formatUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (d > 0) return `${d}日 ${h}時間 ${m}分`;
  if (h > 0) return `${h}時間 ${m}分 ${s}秒`;
  return `${m}分 ${s}秒`;
}

async function loadManagerNodes() {
  const grid = document.getElementById("manager-nodes-grid");
  const badge = document.getElementById("manager-nodes-badge");
  if (!grid) return;

  try {
    const res = await fetch("/api/system/manager_nodes");
    if (!res.ok) {
      const raw = await res.text();
      let err = {};
      try { err = JSON.parse(raw); } catch(_) {}
      grid.innerHTML = `<div style="grid-column:1/-1;padding:24px;text-align:center;color:#ff6b6b;">⚠️ Manager Node情報の取得に失敗しました: ${escapeHtml(err.error || res.statusText || raw.slice(0, 80))}</div>`;
      return;
    }
    const data = await res.json();
    const master = data.master || {};
    const slaves = data.slaves || [];

    if (badge) {
      const activeSlaves = slaves.filter(s => s.status === "online").length;
      const targetBadge = activeSlaves >= 2 ? "🟢 冗長化 完了 (推奨2台達成)" : (activeSlaves === 1 ? "🟡 最小冗長化 (1台待機中 / 推奨2台)" : "⚪ 待機ノードなし (推奨2台)");
      badge.textContent = `主系 1台 / 待機 ${activeSlaves}台 [${targetBadge}]`;
      badge.style.background = activeSlaves >= 2 ? "#3ba55c" : (activeSlaves === 1 ? "#faa81a" : "var(--primary, #5865f2)");
    }

    // 現在のオリジンURLを案内ボックスに反映
    const currentOrigin = window.location.origin;
    document.querySelectorAll(".current-origin-url").forEach(el => {
      el.textContent = currentOrigin;
    });
    const setupBox = document.getElementById("standby-setup-command-box");
    if (setupBox) {
      const haSec = window.currentHaSecret || "";
      setupBox.textContent = `curl -sSL "${currentOrigin}/setup-standby.sh" | sudo bash -s -- "${currentOrigin}" "${haSec}"`;
    }

    let cardsHtml = "";

    // 1. Master カード
    const masterUptime = master.uptime_seconds != null ? formatUptime(master.uptime_seconds) : "-";
    cardsHtml += `
      <div class="card" style="background:rgba(88,101,242,0.08);border:1px solid rgba(88,101,242,0.35);padding:16px;border-radius:10px;position:relative;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
          <div>
            <div style="font-size:12px;color:#7289da;font-weight:700;display:flex;align-items:center;gap:6px;">
              <span>👑 主系ノード (Primary Master)</span>
              <span class="badge" style="background:rgba(88,101,242,0.25);color:#7289da;border:1px solid rgba(88,101,242,0.5);font-weight:700;">★ Priority 0 (最優先本尊)</span>
            </div>
            <h4 style="margin:4px 0 0;font-size:16px;color:var(--text-normal);display:flex;align-items:center;gap:8px;">
              <span>${escapeHtml(master.hostname || "Master Node")}</span>
            </h4>
          </div>
          <span class="badge" style="background:rgba(46,204,113,0.2);color:#2ecc71;border:1px solid rgba(46,204,113,0.4);font-weight:700;">🟢 Active (稼働中)</span>
        </div>

        <div style="font-size:12px;color:var(--text-muted);display:grid;grid-template-columns:1fr 1fr;gap:6px;background:rgba(0,0,0,0.2);padding:10px;border-radius:6px;margin-bottom:12px;">
          <div>IP: <strong style="color:var(--text-normal);">${escapeHtml(master.ip || "-")}</strong></div>
          <div>Version: <strong style="color:var(--text-normal);">v${escapeHtml(master.version || "-")}</strong></div>
          <div>稼働Bot数: <strong style="color:#50e6ff;">${master.active_bots || 1}台</strong></div>
          <div>稼働時間: <strong style="color:var(--text-normal);">${masterUptime}</strong></div>
        </div>

        <div style="font-size:11px;color:var(--text-dim);display:flex;align-items:center;gap:6px;">
          <span>⚡</span>
          <span>Discord VC接続・WebUI・SQLite DB書き込みを担当する主系ノードです。</span>
        </div>
      </div>
    `;

    // 2. Slave カード群
    if (slaves.length === 0) {
      cardsHtml += `
        <div class="card" style="padding:20px;background:rgba(0,0,0,0.15);border:1px dashed rgba(255,255,255,0.12);border-radius:10px;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;">
          <div style="font-size:28px;margin-bottom:8px;">🛡️</div>
          <div style="font-weight:600;font-size:14px;color:var(--text-muted);">Standby (Slave) ノード未登録 (推奨: 2台)</div>
          <div style="font-size:12px;color:var(--text-dim);margin-top:4px;max-width:340px;">
            副系サーバーを用意し、下のワンライナーを実行することで無停止フェイルオーバー対応のHAクラスタ（推奨2台待機構成）を組むことができます。
          </div>
        </div>
      `;
    } else {
      slaves.forEach(slave => {
        let statusBadge = "";
        let borderCol = "rgba(255,255,255,0.08)";
        if (slave.status === "online") {
          statusBadge = `<span class="badge" style="background:rgba(46,204,113,0.2);color:#2ecc71;border:1px solid rgba(46,204,113,0.4);font-weight:700;">🟢 Standby (待機中)</span>`;
          borderCol = "rgba(46,204,113,0.3)";
        } else if (slave.status === "revoked") {
          statusBadge = `<span class="badge" style="background:rgba(231,76,60,0.15);color:#ff6b6b;border:1px solid rgba(231,76,60,0.3);font-weight:700;">🛑 停止・除外済</span>`;
          borderCol = "rgba(231,76,60,0.25)";
        } else {
          statusBadge = `<span class="badge" style="background:rgba(241,196,15,0.2);color:#f1c40f;border:1px solid rgba(241,196,15,0.4);font-weight:700;">⚪ 切断 (${slave.last_seen_ago >= 0 ? slave.last_seen_ago + '秒前' : '未交信'})</span>`;
          borderCol = "rgba(241,196,15,0.25)";
        }

        const prio = slave.priority || 1;
        const failoverSec = 30 + (prio - 1) * 15;
        const priorityBadge = `
          <span class="badge" style="background:${prio === 1 ? 'rgba(88,101,242,0.25)' : 'rgba(155,89,182,0.25)'};color:${prio === 1 ? '#7289da' : '#bb86fc'};border:1px solid ${prio === 1 ? 'rgba(88,101,242,0.5)' : 'rgba(155,89,182,0.5)'};font-weight:700;">
            ★ Priority ${prio} (${prio === 1 ? '最速昇格 約30秒' : `待機昇格 約${failoverSec}秒`})
          </span>
        `;

        const lastSyncText = slave.last_sync_ago >= 0 ? `${slave.last_sync_ago}秒前` : (slave.last_sync > 0 ? new Date(slave.last_sync * 1000).toLocaleTimeString() : "未同期");
        const lastSeenText = slave.last_seen_ago >= 0 ? `${slave.last_seen_ago}秒前` : (slave.last_seen > 0 ? new Date(slave.last_seen * 1000).toLocaleTimeString() : "未交信");

        let actionBtnHtml = "";
        if (slave.status !== "revoked") {
          actionBtnHtml = `
            <button class="btn btn-sm" onclick="revokeManagerSlave('${escapeHtml(slave.node_id)}')" style="background:rgba(231,76,60,0.15);border:1px solid rgba(231,76,60,0.4);color:#ff6b6b;cursor:pointer;font-weight:600;padding:4px 10px;border-radius:6px;transition:all 0.2s;" title="このSlaveノードを除外・停止します。次回アクセス時に410 Goneが送られ安全停止します。">
              🛑 登録解除 & 停止 (Revoke)
            </button>
          `;
        } else {
          actionBtnHtml = `
            <button class="btn btn-sm" onclick="deleteManagerSlave('${escapeHtml(slave.node_id)}')" style="background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.15);color:var(--text-muted);cursor:pointer;padding:4px 10px;border-radius:6px;" title="登録履歴から完全に削除します">
              🗑️ 履歴から削除
            </button>
          `;
        }

        cardsHtml += `
          <div class="card" style="background:rgba(0,0,0,0.2);border:1px solid ${borderCol};padding:16px;border-radius:10px;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;flex-wrap:wrap;gap:8px;">
              <div>
                <div style="font-size:12px;color:var(--text-muted);font-weight:700;display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                  <span>🛡️ 待機ノード (Standby Slave)</span>
                  ${priorityBadge}
                </div>
                <h4 style="margin:4px 0 0;font-size:15px;color:var(--text-normal);">
                  ${escapeHtml(slave.hostname || "Slave Node")}
                </h4>
                <div style="font-size:11px;color:var(--text-dim);font-family:monospace;margin-top:2px;">
                  ID: ${escapeHtml(slave.node_id)}
                </div>
              </div>
              ${statusBadge}
            </div>

            <div style="font-size:12px;color:var(--text-muted);display:grid;grid-template-columns:1fr 1fr;gap:6px;background:rgba(0,0,0,0.25);padding:10px;border-radius:6px;margin-bottom:12px;">
              <div>IP: <strong style="color:var(--text-normal);">${escapeHtml(slave.ip || "-")}</strong></div>
              <div>スペック: <strong style="color:var(--text-normal);">${escapeHtml(slave.spec_display || "-")}</strong></div>
              <div>最終ハートビート: <strong style="color:var(--text-normal);">${lastSeenText}</strong></div>
              <div>最終DB同期: <strong style="color:#50e6ff;">${lastSyncText}</strong></div>
            </div>

            <div style="display:flex;justify-content:space-between;align-items:center;padding-top:8px;border-top:1px solid rgba(255,255,255,0.06);flex-wrap:wrap;gap:8px;">
              <div style="display:flex;align-items:center;gap:6px;font-size:12px;">
                <span style="color:var(--text-muted);">昇格優先度:</span>
                <select onchange="setManagerSlavePriority('${escapeHtml(slave.node_id)}', parseInt(this.value, 10))" style="background:#2b2d31;color:#fff;border:1px solid rgba(255,255,255,0.2);padding:3px 8px;border-radius:4px;font-size:12px;cursor:pointer;">
                  ${[1, 2, 3, 4, 5].map(p => `<option value="${p}" ${p === prio ? 'selected' : ''}>Priority ${p} (${30 + (p-1)*15}s昇格)</option>`).join('')}
                </select>
              </div>
              <div>${actionBtnHtml}</div>
            </div>
          </div>
        `;
      });
    }

    grid.innerHTML = cardsHtml;
  } catch (err) {
    console.error("loadManagerNodes error:", err);
    grid.innerHTML = `<div style="grid-column:1/-1;padding:24px;text-align:center;color:#ff6b6b;">⚠️ Manager Node情報の取得中にエラーが発生しました: ${escapeHtml(err.message)}</div>`;
  }
}

async function setManagerSlavePriority(nodeId, priority) {
  try {
    const res = await fetch("/api/system/manager_nodes/priority", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: nodeId, priority: priority })
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      loadManagerNodes();
    } else {
      alert("優先度の変更に失敗しました: " + (data.error || res.statusText));
    }
  } catch (err) {
    alert("通信エラー: " + err.message);
  }
}
window.setManagerSlavePriority = setManagerSlavePriority;

async function autoSortManagerPriorityBySpec() {
  if (!confirm("接続されている待機 (Slave) ノードの RAM 容量・CPU コア数を自動判定し、ハイスペック順に Priority 1, 2... を割り当てますか？")) {
    return;
  }
  try {
    const res = await fetch("/api/system/manager_nodes/priority", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "auto_spec" })
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      alert(data.message || "スペック順に優先度を再割り当てしました。");
      loadManagerNodes();
    } else {
      alert("スペック整列に失敗しました: " + (data.error || res.statusText));
    }
  } catch (err) {
    alert("通信エラー: " + err.message);
  }
}
window.autoSortManagerPriorityBySpec = autoSortManagerPriorityBySpec;

async function revokeManagerSlave(nodeId) {
  if (!confirm(`⚠️ Manager Slave [${nodeId}] を除外・自律停止しますか？\n\n次回アクセス時に自律停止指令 (HTTP 410 Gone) が送出され、誤昇格（スプリットブレイン）を起こすことなく安全にプロセスが停止します。`)) {
    return;
  }
  try {
    const res = await fetch("/api/system/manager_nodes/revoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: nodeId })
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      alert(data.message || "Slaveを除外・停止対象に登録しました。");
      loadManagerNodes();
    } else {
      alert("除外・停止に失敗しました: " + (data.error || res.statusText));
    }
  } catch (err) {
    alert("通信エラー: " + err.message);
  }
}
window.revokeManagerSlave = revokeManagerSlave;

async function deleteManagerSlave(nodeId) {
  if (!confirm(`Manager Slave [${nodeId}] の登録履歴を完全に削除しますか？`)) {
    return;
  }
  try {
    const res = await fetch(`/api/system/manager_nodes/${encodeURIComponent(nodeId)}`, {
      method: "DELETE"
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      alert(data.message || "登録履歴を削除しました。");
      loadManagerNodes();
    } else {
      alert("削除に失敗しました: " + (data.error || res.statusText));
    }
  } catch (err) {
    alert("通信エラー: " + err.message);
  }
}
window.deleteManagerSlave = deleteManagerSlave;

async function loadWorkerNodes() {
  try {
    const res = await fetch("/api/worker/nodes");
    if (!res.ok) return;
    const data = await res.json();
    if (data.pools) {
      renderWorkerNodes(data.pools);
    }
  } catch (err) {
    console.error("loadWorkerNodes error:", err);
  }
}
window.loadWorkerNodes = loadWorkerNodes;

async function kickWorkerNode(nodeId, ip, btn) {
  if (!confirm(`Workerノード [${nodeId || ip}] を切断・キックしますか？\nボランティア接続の場合はWebSocketが即座にクローズされます。`)) {
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/worker/kick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: nodeId, ip: ip })
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      alert(data.message || "Workerノードを切断しました。");
      loadWorkerNodes();
    } else {
      alert("切断に失敗しました: " + (data.error || res.statusText));
    }
  } catch (err) {
    alert("通信エラー: " + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}
window.kickWorkerNode = kickWorkerNode;

// ==========================================
// 2. Discord Bot プール管理 (1〜10号機)
// ==========================================
async function loadBotSlots() {
  const grid = document.getElementById("bot-slots-grid");
  const badge = document.getElementById("bots-summary-badge");
  if (!grid) return;

  try {
    const res = await fetch("/api/system/bots");
    if (!res.ok) {
      const raw = await res.text();
      let err = {};
      try { err = JSON.parse(raw); } catch(_) {}
      grid.innerHTML = `<div style="grid-column:1/-1;padding:24px;text-align:center;color:#ff6b6b;">⚠️ Bot情報の取得に失敗しました: ${escapeHtml(err.error || res.statusText || raw.slice(0, 80))}</div>`;
      return;
    }
    const data = await res.json();
    const slots = data.slots || data.bots || [];

    if (badge) {
      const runningCnt = data.running_count !== undefined ? data.running_count : slots.filter(s => s.is_running || s.is_ready).length;
      const enabledCnt = data.enabled_count !== undefined ? data.enabled_count : slots.filter(s => s.is_enabled).length;
      const totalSlots = data.total_slots || slots.length || 10;
      badge.textContent = `稼働中: ${runningCnt} / ${enabledCnt} 台 (全${totalSlots}スロット)`;
      badge.style.background = runningCnt > 0 ? "var(--primary, #5865f2)" : "rgba(255,255,255,0.15)";
    }

    grid.innerHTML = slots.map(slot => {
      const idx = slot.index !== undefined ? slot.index : slot.bot_index;
      const isConfigured = slot.is_configured !== undefined ? slot.is_configured : Boolean(slot.is_registered);
      const isRunning = Boolean(slot.is_running);
      const isEnabled = Boolean(slot.is_enabled);
      const isPrimary = idx === 1;
      const tokenMasked = slot.token_masked || slot.masked_token || 'トークン未設定';
      const guildsCount = slot.guilds_count !== undefined ? slot.guilds_count : (slot.guild_count || 0);

      let statusBadge = "";
      let borderColor = "rgba(255,255,255,0.08)";
      if (!isConfigured) {
        statusBadge = `<span class="badge" style="background:rgba(255,255,255,0.06);color:var(--text-dim, #72767d);">未登録</span>`;
      } else if (isRunning) {
        statusBadge = `<span class="badge" style="background:rgba(46,204,113,0.2);color:#2ecc71;border:1px solid rgba(46,204,113,0.4);">🟢 接続中 (${Math.round(slot.ping_ms || 0)}ms)</span>`;
        borderColor = "rgba(46,204,113,0.3)";
      } else if (isEnabled) {
        statusBadge = `<span class="badge" style="background:rgba(241,196,15,0.2);color:#f1c40f;border:1px solid rgba(241,196,15,0.4);">🟡 接続準備中...</span>`;
        borderColor = "rgba(241,196,15,0.3)";
      } else {
        statusBadge = `<span class="badge" style="background:rgba(231,76,60,0.15);color:#ff6b6b;border:1px solid rgba(231,76,60,0.3);">⚪ 停止中</span>`;
      }

      const roleBadge = isPrimary
        ? `<span class="badge" style="background:rgba(88,101,242,0.25);color:#7289da;border:1px solid rgba(88,101,242,0.4);">👑 代表Bot / WebUI</span>`
        : `<span class="badge" style="background:rgba(255,255,255,0.06);color:var(--text-muted, #999);">⚡ 多重化ワーカー</span>`;

      let guildsListHtml = "";
      if (slot.guilds && slot.guilds.length > 0) {
        guildsListHtml = `
          <div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.05);font-size:11px;color:var(--text-muted);">
            参加サーバー (${guildsCount}件):
            <div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px;">
              ${slot.guilds.map(g => `<span style="background:rgba(0,0,0,0.3);padding:2px 6px;border-radius:4px;color:var(--text-light);">${escapeHtml(g.name)}</span>`).join("")}
            </div>
          </div>
        `;
      }

      return `
        <div class="card" style="margin:0;padding:16px;background:rgba(0,0,0,0.2);border:1px solid ${borderColor};border-radius:8px;display:flex;flex-direction:column;justify-content:space-between;">
          <div>
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:8px;">
              <div>
                <div style="font-weight:700;font-size:14px;color:var(--text-light);display:flex;align-items:center;gap:6px;">
                  <span>${idx}号機</span>
                  ${roleBadge}
                </div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:2px;">
                  ${slot.label ? escapeHtml(slot.label) : '<span style="color:var(--text-dim);font-style:italic;">ラベルなし</span>'}
                </div>
              </div>
              <div>${statusBadge}</div>
            </div>

            <div style="font-size:11px;font-family:monospace;color:var(--text-dim);background:rgba(0,0,0,0.25);padding:4px 8px;border-radius:4px;margin:8px 0;word-break:break-all;">
              ${tokenMasked}
            </div>

            ${guildsListHtml}
          </div>

          <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.08);">
            <div>
              ${isConfigured ? `
                <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:var(--text-light);">
                  <input type="checkbox" ${isEnabled ? 'checked' : ''} onchange="toggleBotSlot(${idx}, this.checked)">
                  <span>${isEnabled ? '有効' : '無効'}</span>
                </label>
              ` : `
                <span style="font-size:12px;color:var(--text-dim);">未設定</span>
              `}
            </div>
            <div style="display:flex;gap:6px;">
              <button type="button" class="btn btn-secondary btn-sm" onclick="openAddBotModal(${idx}, '${escapeHtml(slot.label || '')}')" style="padding:4px 8px;font-size:11px;">
                ${isConfigured ? '編集' : '＋ 設定'}
              </button>
              ${isConfigured ? `
                <button type="button" class="btn btn-danger btn-sm" onclick="deleteBotSlot(${idx})" style="padding:4px 8px;font-size:11px;" title="トークンを削除">
                  🗑️
                </button>
              ` : ''}
            </div>
          </div>
        </div>
      `;
    }).join("");
  } catch (err) {
    console.error("loadBotSlots error:", err);
    grid.innerHTML = `<div style="grid-column:1/-1;padding:24px;text-align:center;color:#ff6b6b;">⚠️ Bot情報の取得中にエラーが発生しました: ${escapeHtml(err.message)}</div>`;
  }
}
window.loadBotSlots = loadBotSlots;

function openAddBotModal(idx = 1, label = "") {
  const modal = document.getElementById("bot-token-modal");
  const idxSelect = document.getElementById("bot-modal-index");
  const labelInput = document.getElementById("bot-modal-label");
  const tokenInput = document.getElementById("bot-modal-token");
  const enabledInput = document.getElementById("bot-modal-enabled");
  const title = document.getElementById("bot-modal-title");

  if (!modal) return;
  if (idxSelect) idxSelect.value = String(idx);
  if (labelInput) labelInput.value = label;
  if (tokenInput) tokenInput.value = "";
  if (enabledInput) enabledInput.checked = true;
  if (title) title.textContent = `Botトークンの設定 (${idx}号機)`;

  modal.style.display = "flex";
}
window.openAddBotModal = openAddBotModal;

function closeBotModal() {
  const modal = document.getElementById("bot-token-modal");
  if (modal) modal.style.display = "none";
}
window.closeBotModal = closeBotModal;

async function submitBotTokenForm(event) {
  event.preventDefault();
  const idx = parseInt(document.getElementById("bot-modal-index").value, 10);
  const label = document.getElementById("bot-modal-label").value.trim();
  const token = document.getElementById("bot-modal-token").value.trim();
  const enabled = document.getElementById("bot-modal-enabled").checked;
  const submitBtn = document.getElementById("bot-modal-submit-btn");

  if (!token) {
    showToast("Discord Bot Token を入力してください", "error");
    return;
  }

  if (submitBtn) submitBtn.disabled = true;

  try {
    const res = await fetch("/api/system/bots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bot_index: idx,
        token: token,
        label: label,
        is_enabled: enabled
      })
    });
    const rawText = await res.text();
    let data = {};
    try {
      data = JSON.parse(rawText);
    } catch (_) {
      showToast(`サーバーエラー (${res.status}): ${rawText.slice(0, 100)}`, "error");
      return;
    }

    if (res.ok && (data.status === "ok" || data.success)) {
      showToast(`${idx}号機トークンを設定しました 🚀`, "success");
      closeBotModal();
      loadBotSlots();
    } else {
      showToast(data.error || "Bot設定の保存に失敗しました", "error");
    }
  } catch (err) {
    showToast(`通信エラー: ${err.message}`, "error");
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}
window.submitBotTokenForm = submitBotTokenForm;

async function toggleBotSlot(idx, enabled) {
  try {
    const res = await fetch(`/api/system/bots/${idx}/toggle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_enabled: enabled })
    });
    const rawText = await res.text();
    let data = {};
    try {
      data = JSON.parse(rawText);
    } catch (_) {
      showToast(`サーバーエラー (${res.status}): ${rawText.slice(0, 100)}`, "error");
      return;
    }

    if (res.ok && (data.status === "ok" || data.success)) {
      showToast(enabled ? `${idx}号機の起動を開始しました` : `${idx}号機を停止しました`, "success");
      loadBotSlots();
    } else {
      showToast(data.error || "状態変更に失敗しました", "error");
      loadBotSlots();
    }
  } catch (err) {
    showToast(`通信エラー: ${err.message}`, "error");
    loadBotSlots();
  }
}
window.toggleBotSlot = toggleBotSlot;

async function deleteBotSlot(idx) {
  if (!confirm(`${idx}号機の登録トークンを削除し、Botを切断・停止しますか？`)) {
    return;
  }
  try {
    const res = await fetch(`/api/system/bots/${idx}`, {
      method: "DELETE"
    });
    const rawText = await res.text();
    let data = {};
    try {
      data = JSON.parse(rawText);
    } catch (_) {
      showToast(`サーバーエラー (${res.status}): ${rawText.slice(0, 100)}`, "error");
      return;
    }

    if (res.ok && (data.status === "ok" || data.success)) {
      showToast(`${idx}号機を削除しました 🗑️`, "success");
      loadBotSlots();
    } else {
      showToast(data.error || "削除に失敗しました", "error");
    }
  } catch (err) {
    showToast(`通信エラー: ${err.message}`, "error");
  }
}
window.deleteBotSlot = deleteBotSlot;

function generateBotInviteLink() {
  const input = document.getElementById("invite-client-id-input");
  if (!input) return;
  const clientId = input.value.trim();
  if (!clientId) {
    showToast("Application Client ID を入力してください", "error");
    input.focus();
    return;
  }
  const url = `https://discord.com/api/oauth2/authorize?client_id=${encodeURIComponent(clientId)}&permissions=3148800&scope=bot%20applications.commands`;
  window.open(url, "_blank");
}
window.generateBotInviteLink = generateBotInviteLink;

// ==========================================
// 3. API・クレデンシャル設定 & Worker連携
// ==========================================
function updateWorkerOneliner(webBaseUrl, secret) {
  const pre = document.getElementById("worker-oneliner-code");
  if (!pre) return;

  const baseUrl = (webBaseUrl || window.location.origin).replace(/\/$/, "");
  let wsUrl = baseUrl.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
  if (!wsUrl.includes("/api/worker/ws")) {
    wsUrl = `${wsUrl}/api/worker/ws`;
  }
  const sec = secret || "your_worker_secret_here";
  const cmd = `curl -sSL "${baseUrl}/static/setup-worker.sh" | bash -s -- --master-ws "${wsUrl}" --secret "${sec}"`;
  pre.textContent = cmd;
}

function updateStandbyOneliner() {
  const shPre = document.getElementById("standby-oneliner-sh");
  const ps1Pre = document.getElementById("standby-oneliner-ps1");
  const setupBox = document.getElementById("standby-setup-command-box");
  if (!shPre && !ps1Pre && !setupBox) return;

  const origin = window.location.origin.replace(/\/$/, "");

  const shCmd = `curl -sSL "${origin}/setup-standby.sh" | sudo bash`;
  const ps1Cmd = `irm "${origin}/setup-standby.ps1" | iex`;

  if (shPre) shPre.textContent = shCmd;
  if (ps1Pre) ps1Pre.textContent = ps1Cmd;
  if (setupBox) setupBox.textContent = shCmd;
}
window.updateStandbyOneliner = updateStandbyOneliner;

function updateVolunteerOneliner() {
  const ps1Pre = document.getElementById("volunteer-ps1-code");
  const shPre = document.getElementById("volunteer-sh-code");
  if (!ps1Pre && !shPre) return;

  const origin = window.location.origin.replace(/\/$/, "");
  let wsUrl = origin.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
  if (!wsUrl.includes("/api/worker/ws")) {
    wsUrl = `${wsUrl}/api/worker/ws`;
  }

  if (ps1Pre) {
    ps1Pre.textContent = `$env:TTSBOT_SERVER_URL="${wsUrl}"; irm "${origin}/volunteer/run_volunteer_gui.ps1" | iex`;
  }
  if (shPre) {
    shPre.textContent = `curl -sSL "${origin}/volunteer/run_volunteer.sh" | bash -s -- "${wsUrl}"`;
  }
}
window.updateVolunteerOneliner = updateVolunteerOneliner;

async function loadCredentials() {
  const azureBadge = document.getElementById("azure-status-badge");
  const googleBadge = document.getElementById("google-status-badge");
  const azureRegionInput = document.getElementById("cred-azure-region");
  const workerSecretInput = document.getElementById("cred-worker-secret");
  const webBaseUrlInput = document.getElementById("cred-web-base-url");

  // Cloudflare Tunnel
  const tunnelBadge = document.getElementById("tunnel-status-badge");
  const tunnelInput = document.getElementById("cred-cloudflare-tunnel-token");
  const tunnelTokenState = document.getElementById("tunnel-token-state");
  const btnRestartTunnel = document.getElementById("btn-restart-tunnel");
  const btnStopTunnel = document.getElementById("btn-stop-tunnel");
  const btnClearToken = document.getElementById("btn-clear-tunnel-token");
  const tunnelRuntimeInfo = document.getElementById("tunnel-runtime-info");
  const tunnelStateText = document.getElementById("tunnel-container-state-text");
  const tunnelIdSpan = document.getElementById("tunnel-container-id-span");
  const tunnelIdText = document.getElementById("tunnel-container-id-text");

  try {
    const res = await fetch("/api/system/credentials");
    if (!res.ok) {
      const err = await res.json();
      showToast(`クレデンシャル取得エラー: ${err.error || res.statusText}`, "error");
      return;
    }
    const data = await res.json();

    if (azureBadge) {
      azureBadge.textContent = data.azure_speech_configured ? "🟢 設定済み" : "⚪ 未設定";
      azureBadge.style.background = data.azure_speech_configured ? "rgba(46,204,113,0.2)" : "rgba(255,255,255,0.06)";
      azureBadge.style.color = data.azure_speech_configured ? "#2ecc71" : "var(--text-dim)";
    }
    if (azureRegionInput) {
      azureRegionInput.value = data.azure_speech_region || "japaneast";
    }

    if (googleBadge) {
      const gOk = data.google_tts_configured || data.google_configured;
      googleBadge.textContent = gOk ? (data.has_google_json ? "🟢 JSONキー設定済" : "🟢 APIキー設定済") : "⚪ 未設定";
      googleBadge.style.background = gOk ? "rgba(46,204,113,0.2)" : "rgba(255,255,255,0.06)";
      googleBadge.style.color = gOk ? "#2ecc71" : "var(--text-dim)";
    }

    if (workerSecretInput) {
      workerSecretInput.value = data.worker_shared_secret || "";
    }
    if (webBaseUrlInput) {
      webBaseUrlInput.value = data.web_base_url || window.location.origin;
    }

    // Cloudflare Tunnel のステータス反映
    if (tunnelBadge) {
      const isConfigured = !!data.cloudflare_tunnel_configured;
      const isRunning = !!data.cloudflare_tunnel_running;
      const tStatus = data.cloudflare_tunnel_status || "not_created";

      if (isConfigured && isRunning) {
        tunnelBadge.textContent = "🟢 稼働中";
        tunnelBadge.style.background = "rgba(46,204,113,0.2)";
        tunnelBadge.style.color = "#2ecc71";
      } else if (isConfigured && !isRunning) {
        tunnelBadge.textContent = "🟡 停止中";
        tunnelBadge.style.background = "rgba(241,196,15,0.2)";
        tunnelBadge.style.color = "#f1c40f";
      } else {
        tunnelBadge.textContent = "⚪ 未設定";
        tunnelBadge.style.background = "rgba(255,255,255,0.06)";
        tunnelBadge.style.color = "var(--text-dim)";
      }

      if (btnRestartTunnel) {
        btnRestartTunnel.style.display = isConfigured ? "inline-block" : "none";
      }
      if (btnStopTunnel) {
        btnStopTunnel.style.display = (isConfigured && isRunning) ? "inline-block" : "none";
      }
      if (btnClearToken) {
        btnClearToken.style.display = isConfigured ? "inline-block" : "none";
      }

      if (tunnelTokenState) {
        tunnelTokenState.textContent = isConfigured ? `(設定済み: ${data.cloudflare_tunnel_token})` : "(未設定)";
      }
      if (tunnelInput) {
        tunnelInput.placeholder = isConfigured ? "設定済み (変更する場合のみ新しいTokenを入力)" : "未設定 (入力して保存すると自動起動します)";
      }

      if (tunnelRuntimeInfo) {
        if (tStatus !== "not_created") {
          tunnelRuntimeInfo.style.display = "block";
          if (tunnelStateText) {
            tunnelStateText.textContent = isRunning ? "running (正常稼働)" : tStatus;
            tunnelStateText.style.color = isRunning ? "#2ecc71" : "#e67e22";
          }
          if (tunnelIdSpan && tunnelIdText) {
            if (data.cloudflare_tunnel_container_id) {
              tunnelIdSpan.style.display = "inline";
              tunnelIdText.textContent = data.cloudflare_tunnel_container_id;
            } else {
              tunnelIdSpan.style.display = "none";
            }
          }
        } else {
          tunnelRuntimeInfo.style.display = "none";
        }
      }
    }

    updateWorkerOneliner(data.web_base_url, data.worker_shared_secret);

    // システム全体エラー通知先
    const credErrorContact = document.getElementById("cred-error-contact-user-id");
    if (credErrorContact) {
      credErrorContact.value = (data.error_contact_user_id && data.error_contact_user_id !== "0") ? String(data.error_contact_user_id) : "";
    }

    // HA Secret の保持とワンライナー更新
    if (data.ha_secret) {
      window.currentHaSecret = data.ha_secret;
      updateStandbyOneliner(data.ha_secret);
    }
  } catch (err) {
    console.error("loadCredentials error:", err);
    showToast(`クレデンシャル読み込み失敗: ${err.message}`, "error");
  }
}
window.loadCredentials = loadCredentials;

function generateNewWorkerSecret() {
  const chars = "abcdef0123456789";
  let secret = "";
  const cryptoObj = window.crypto || window.msCrypto;
  if (cryptoObj && cryptoObj.getRandomValues) {
    const arr = new Uint8Array(32);
    cryptoObj.getRandomValues(arr);
    secret = Array.from(arr, b => b.toString(16).padStart(2, '0')).join('');
  } else {
    for (let i = 0; i < 64; i++) {
      secret += chars.charAt(Math.floor(Math.random() * chars.length));
    }
  }

  const input = document.getElementById("cred-worker-secret");
  const webBaseUrlInput = document.getElementById("cred-web-base-url");
  if (input) input.value = secret;
  updateWorkerOneliner(webBaseUrlInput ? webBaseUrlInput.value : "", secret);
  showToast("新しい秘密鍵を生成しました（保存するまで反映されません）", "success");
}
window.generateNewWorkerSecret = generateNewWorkerSecret;

function copyWorkerOneliner() {
  const pre = document.getElementById("worker-oneliner-code");
  if (!pre) return;
  const text = pre.textContent;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => {
      showToast("ワンライナーコマンドをコピーしました 📋", "success");
    }).catch(err => {
      showToast("コピーに失敗しました", "error");
    });
  } else {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    document.body.removeChild(textarea);
    showToast("ワンライナーコマンドをコピーしました 📋", "success");
  }
}
window.copyWorkerOneliner = copyWorkerOneliner;

// パスワード/トークン表示・非表示トグル
function togglePasswordVisibility(inputId, btn) {
  const input = document.getElementById(inputId);
  if (!input) return;
  if (input.type === "password") {
    input.type = "text";
    if (btn) btn.textContent = "🔒";
  } else {
    input.type = "password";
    if (btn) btn.textContent = "👁️";
  }
}
window.togglePasswordVisibility = togglePasswordVisibility;

async function restartTunnelService() {
  if (!confirm("Cloudflare Tunnel を再起動しますか？")) return;
  showToast("🔄 Cloudflare Tunnel を再起動中...", "info");
  try {
    const res = await fetch("/api/system/tunnel/restart", { method: "POST" });
    const data = await res.json();
    if (res.ok && (data.status === "ok" || data.success)) {
      showToast(data.message || "Cloudflare Tunnel を再起動しました", "success");
      loadCredentials();
    } else {
      showToast(data.error || "Tunnel再起動に失敗しました", "error");
    }
  } catch (err) {
    showToast(`通信エラー: ${err.message}`, "error");
  }
}
window.restartTunnelService = restartTunnelService;

async function stopTunnelService() {
  if (!confirm("Cloudflare Tunnel を停止しますか？\n外部（WAN）からのアクセスが遮断されます。")) return;
  showToast("⏹️ Cloudflare Tunnel を停止中...", "info");
  try {
    const res = await fetch("/api/system/tunnel/stop", { method: "POST" });
    const data = await res.json();
    if (res.ok && (data.status === "ok" || data.success)) {
      showToast(data.message || "Cloudflare Tunnel を停止しました", "success");
      loadCredentials();
    } else {
      showToast(data.error || "Tunnel停止に失敗しました", "error");
    }
  } catch (err) {
    showToast(`通信エラー: ${err.message}`, "error");
  }
}
window.stopTunnelService = stopTunnelService;

async function clearTunnelToken() {
  if (!confirm("Cloudflare Tunnel Token をクリアしてトンネルを停止しますか？\n外部公開用トンネルが停止・破棄されます。")) return;
  showToast("🗑️ TokenをクリアしTunnelを停止中...", "info");
  try {
    const res = await fetch("/api/system/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cloudflare_tunnel_token: "" })
    });
    const data = await res.json();
    if (res.ok && (data.status === "ok" || data.success)) {
      showToast("Tunnel Token を削除し、トンネルを停止しました", "success");
      const inp = document.getElementById("cred-cloudflare-tunnel-token");
      if (inp) inp.value = "";
      loadCredentials();
    } else {
      showToast(data.error || "Token削除に失敗しました", "error");
    }
  } catch (err) {
    showToast(`通信エラー: ${err.message}`, "error");
  }
}
window.clearTunnelToken = clearTunnelToken;

async function saveCredentials(event) {
  event.preventDefault();
  const azureKey = document.getElementById("cred-azure-key")?.value?.trim() || "";
  const azureRegion = document.getElementById("cred-azure-region")?.value?.trim() || "japaneast";
  const googleJson = document.getElementById("cred-google-json")?.value?.trim() || "";
  const googleKey = document.getElementById("cred-google-key")?.value?.trim() || "";
  const workerSecret = document.getElementById("cred-worker-secret")?.value?.trim() || "";
  const webBaseUrl = document.getElementById("cred-web-base-url")?.value?.trim() || "";
  const adminPassword = document.getElementById("cred-admin-password")?.value?.trim() || "";
  const cfTunnelToken = document.getElementById("cred-cloudflare-tunnel-token")?.value?.trim();

  const payload = {
    azure_speech_region: azureRegion,
    worker_shared_secret: workerSecret,
    web_base_url: webBaseUrl
  };
  if (azureKey) payload.azure_speech_key = azureKey;
  if (googleJson) {
    payload.google_credentials_json = googleJson;
    payload.google_service_account_json = googleJson;
  }
  if (googleKey) payload.google_api_key = googleKey;
  if (adminPassword) payload.admin_password = adminPassword;
  if (cfTunnelToken !== undefined && cfTunnelToken !== "") {
    payload.cloudflare_tunnel_token = cfTunnelToken;
  }
  const credErrorContactVal = document.getElementById("cred-error-contact-user-id")?.value?.trim();
  if (credErrorContactVal !== undefined) {
    payload.error_contact_user_id = credErrorContactVal;
  }

  try {
    const res = await fetch("/api/system/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const rawText = await res.text();
    let data = {};
    try {
      data = JSON.parse(rawText);
    } catch (_) {
      showToast(`サーバーエラー (${res.status}): ${rawText.slice(0, 120)}`, "error");
      return;
    }

    if (res.ok && (data.status === "ok" || data.success)) {
      showToast(data.message || "設定を保存し、ホットリロードを完了しました 💾", "success");
      const k1 = document.getElementById("cred-azure-key");
      const k2 = document.getElementById("cred-google-json");
      const k3 = document.getElementById("cred-google-key");
      const k4 = document.getElementById("cred-admin-password");
      const k5 = document.getElementById("cred-cloudflare-tunnel-token");
      if (k1) k1.value = "";
      if (k2) k2.value = "";
      if (k3) k3.value = "";
      if (k4) k4.value = "";
      if (k5) k5.value = "";
      loadCredentials();
    } else {
      showToast(data.error || "設定保存に失敗しました", "error");
    }
  } catch (err) {
    showToast(`通信エラー: ${err.message}`, "error");
  }
}
window.saveCredentials = saveCredentials;

// 障害・エラー通知先へのテストDM送信
async function testErrorAlert(isGuild) {
  const btn = document.getElementById(isGuild ? "btn-test-error-alert-guild" : "btn-test-error-alert-system");
  const input = document.getElementById(isGuild ? "cfg-error-contact-user-id" : "cred-error-contact-user-id");
  const originalHtml = btn ? btn.innerHTML : "";

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span>⏳ 送信中...</span>';
  }

  const targetUserId = input ? input.value.trim() : "";
  const payload = {};
  if (isGuild && typeof currentGuildId !== "undefined" && currentGuildId) {
    payload.guild_id = currentGuildId;
  }
  if (targetUserId) {
    payload.target_user_id = targetUserId;
  }

  try {
    const res = await fetch("/api/system/alert/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (res.ok && (data.status === "ok" || data.success)) {
      showToast(data.message || `Discord ユーザー (ID: ${data.target_user_id}) 宛てにテストDMを送信しました！`, "success");
    } else {
      showToast(data.error || "テスト送信に失敗しました", "error");
    }
  } catch (err) {
    showToast(`通信エラー: ${err.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalHtml;
    }
  }
}
window.testErrorAlert = testErrorAlert;

async function testCredential(provider) {
  showToast(`⚡ ${provider.toUpperCase()} のテスト音声合成を実行中...`, "success");
  try {
    const res = await fetch("/api/system/credentials/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: provider })
    });
    const rawText = await res.text();
    let data = {};
    try {
      data = JSON.parse(rawText);
    } catch (_) {
      showToast(`サーバーエラー (${res.status}): ${rawText.slice(0, 120)}`, "error");
      return;
    }

    if (res.ok && (data.status === "ok" || data.success)) {
      showToast(`✅ ${provider.toUpperCase()} 疎通成功！ (遅延: ${data.latency_ms}ms, 形式: ${data.format})`, "success");
      if (data.audio_base64) {
        const audio = new Audio(`data:audio/${data.format};base64,${data.audio_base64}`);
        audio.play().catch(e => console.warn("Audio play prevented:", e));
      }
    } else {
      showToast(`❌ ${provider.toUpperCase()} テスト失敗: ${data.error || data.message || res.statusText}`, "error");
    }
  } catch (err) {
    showToast(`通信エラー: ${err.message}`, "error");
  }
}
window.testCredential = testCredential;
