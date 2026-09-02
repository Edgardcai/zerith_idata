"use strict";

/*
 * Frontend API contract
 * ---------------------
 * GET  /api/config
 *   { motors: [{id,key,label,group,min,max,unit,step}],
 *     chassis: {wheel_speed:{unit:"rad/s",default:number,min:null,max:null}} }
 * GET  /api/state
 *   { connected,sdk_loaded,takeover,control_mode,init_state,
 *     motors: {"7":{position,speed,error_flag}} }
 * POST /api/takeover          {enabled:boolean,client_id:string}
 * POST /api/motion/joint      {motor_id:number,target:number,speed_scale:number}
 * POST /api/motion/chassis    {left_speed:number,right_speed:number}
 * POST /api/heartbeat
 * POST /api/actions/init | /api/actions/deinit | /api/actions/home {speed_scale:number}
 * POST /api/stop
 * GET  /api/voice/status
 * GET  /api/voice/audio/{id}.wav
 * POST /api/voice/start
 * POST /api/voice/finish-input
 * POST /api/voice/text         {text:string,language:"zh"|"en"}
 * POST /api/voice/cancel
 * WS   /api/voice/asr/ws       browser 16kHz mono PCM16; partial/final JSON
 * POST /api/voice/motion       {enabled:boolean}
 * GET  /api/pi05/status
 * POST /api/pi05/probe         {}
 * POST /api/pi05/reconnect     {host:string,port:number}
 * POST /api/pi05/disconnect    {}
 * POST /api/pi05/dry-run       {prompt:string} + control lease
 * POST /api/pi05/start         {prompt:string,control_rate_hz:number,
 *                               steps_per_chunk:number,
 *                               joint_speed_deg_s:number,
 *                               confirmation:string} + control lease
 * POST /api/pi05/stop          {}
 * POST /api/pi05/reset-fault   {}
 * WS   /api/cameras/ws
 *   subscribe text: {streams:["left_wrist/rgb", "head/depth", ...]}
 *   frame binary: [one-byte stream id][complete JPEG bytes]
 * Optional MJPEG fallback routes may exist at
 * /api/cameras/{left_wrist|head|right_wrist}/{rgb|depth}.mjpg, but this UI
 * deliberately uses one WebSocket so camera streams cannot exhaust the
 * browser's HTTP/1.1 per-origin connection pool.
 *
 * All limits come from /api/config. If the backend does not provide a finite SDK
 * min/max pair, that actuator is intentionally not rendered as controllable.
 */

const API = Object.freeze({
  config: "/api/config",
  state: "/api/state",
  takeover: "/api/takeover",
  joint: "/api/motion/joint",
  chassis: "/api/motion/chassis",
  heartbeat: "/api/heartbeat",
  stop: "/api/stop",
  action: (name) => `/api/actions/${name}`,
  cameraWs: "/api/cameras/ws",
  voiceStatus: "/api/voice/status",
  voiceStart: "/api/voice/start",
  voiceFinishInput: "/api/voice/finish-input",
  voiceText: "/api/voice/text",
  voiceCancel: "/api/voice/cancel",
  voiceAsrWs: "/api/voice/asr/ws",
  voiceMotion: "/api/voice/motion",
  voiceAudio: (id) => `/api/voice/audio/${encodeURIComponent(id)}.wav`,
  pi05Status: "/api/pi05/status",
  pi05Probe: "/api/pi05/probe",
  pi05Reconnect: "/api/pi05/reconnect",
  pi05Disconnect: "/api/pi05/disconnect",
  pi05DryRun: "/api/pi05/dry-run",
  pi05Start: "/api/pi05/start",
  pi05Stop: "/api/pi05/stop",
  pi05ResetFault: "/api/pi05/reset-fault",
});

const PI05_CONFIRMATION = "我确认实体急停可用并启动PI0.5真机执行";
const PI05_CONTROL_UNLOCKED_PHASES = new Set(["idle", "probing", "dry_run_ready"]);
const PI05_CONFIGURATION_PHASES = new Set(["idle", "dry_run_ready"]);
const PI05_CAMERA_UI = Object.freeze([
  { wire: "cam_high", service: "head", client: "rs/cam_high", mapId: "pi05CameraMapHigh", ageId: "pi05CameraAgeHigh" },
  { wire: "cam_left_wrist", service: "left_wrist", client: "rs/cam_left_wrist", mapId: "pi05CameraMapLeft", ageId: "pi05CameraAgeLeft" },
  { wire: "cam_right_wrist", service: "right_wrist", client: "rs/cam_right_wrist", mapId: "pi05CameraMapRight", ageId: "pi05CameraAgeRight" },
]);

const GROUP_TARGETS = Object.freeze({
  left_arm: "leftArmControls",
  right_arm: "rightArmControls",
  body: "bodyControls",
});

const GROUP_ALIASES = Object.freeze({
  left: "left_arm",
  left_arm: "left_arm",
  right: "right_arm",
  right_arm: "right_arm",
  lift: "body",
  waist: "body",
  head: "body",
  body: "body",
});

function createPageClientId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  window.crypto?.getRandomValues?.(bytes);
  return `page-${Date.now().toString(36)}-${[...bytes].map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

const state = {
  configLoaded: false,
  connected: false,
  sdkLoaded: false,
  backendBusy: false,
  localBusy: false,
  takeover: false,
  takeoverPending: false,
  remoteTakeover: false,
  clientId: createPageClientId(),
  leaseId: null,
  heartbeatTimer: null,
  heartbeatFailures: 0,
  cameraSocket: null,
  cameraReconnectTimer: null,
  cameraReconnectDelay: 500,
  cameraObjectUrls: new Map(),
  driveTimer: null,
  driveDirection: null,
  chassisInFlight: false,
  pendingChassis: null,
  chassisFlushPromise: Promise.resolve(),
  stateTimer: null,
  lastStateAt: 0,
  activeModalResolve: null,
  motorElements: new Map(),
  motionSpeedConfig: null,
  voiceTimer: null,
  voiceOnline: false,
  voiceStartPending: false,
  voiceTextPending: false,
  voiceSequence: null,
  voiceSessionId: null,
  latestVoiceAudioId: null,
  voiceState: "offline",
  voiceMotionEnabled: false,
  voiceMotionReady: false,
  voiceMotionPending: false,
  voiceBrowserListening: false,
  voiceBrowserFinishing: false,
  voiceSocket: null,
  voiceMediaStream: null,
  voiceAudioContext: null,
  voiceAudioSource: null,
  voiceAudioWorklet: null,
  voiceAudioSink: null,
  voiceReconnectTimer: null,
  voiceReconnectDelay: 300,
  initState: null,
  controlModeName: "--",
  pi05Timer: null,
  pi05Status: null,
  pi05StatusOnline: false,
  pi05ActionPending: false,
  pi05StartPending: false,
  pi05StopPending: false,
  pi05Metadata: null,
  pi05DryRunResult: null,
  pi05EndpointDirty: false,
};

const dom = {};

document.addEventListener("DOMContentLoaded", () => {
  cacheDom();
  bindTabs();
  bindTakeover();
  bindLifecycleActions();
  bindStopControls();
  bindDriveControls();
  bindCameras();
  bindVoice();
  bindPi05();
  bindModal();
  window.addEventListener("blur", stopDrive);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopDrive();
  });
  window.addEventListener("pagehide", () => {
    window.clearTimeout(state.voiceTimer);
    window.clearTimeout(state.pi05Timer);
    stopChineseAudioCapture();
    closeChineseVoiceSocket();
    closeCameraSocket();
    releaseOnExit();
  });
  void bootstrap();
});

function cacheDom() {
  [
    "appShell", "connectionChip", "connectionText", "sdkText", "modeText",
    "takeoverToggle", "takeoverHint", "stopButton", "initButton", "deinitButton",
    "homeButton", "leftArmControls", "rightArmControls", "leftGripperControl",
    "rightGripperControl", "bodyControls", "speedControls", "driveState",
    "motionSpeedRange", "motionSpeedValue",
    "driveStopButton", "cameraStack", "rgbCamerasToggle", "depthCamerasToggle",
    "confirmModal", "confirmTitle", "confirmMessage", "confirmDetail",
    "confirmIcon", "confirmCancel", "confirmAccept", "toastRegion",
    "voiceStateBadge", "voiceStateText", "voiceDetail", "voiceOrb",
    "voiceStartButton", "voiceStartText", "voiceCancelButton", "voiceTranscript", "voiceAudio",
    "voiceAutoplay", "voiceLanguage", "voiceMotionToggle", "voiceMotionHint",
    "voiceTextForm", "voiceTextInput", "voiceTextSend", "voiceTextHint", "voiceInputHint",
    "pi05PhaseBadge", "pi05PhaseText", "pi05PollState", "pi05Endpoint",
    "pi05Host", "pi05Port", "pi05Prompt", "pi05JointSpeed", "pi05ControlRate", "pi05StepsPerChunk",
    "pi05ProbeButton", "pi05DryRunButton", "pi05StartButton",
    "pi05EmergencyStopButton", "pi05DisconnectButton", "pi05ReconnectButton",
    "pi05StopButton", "pi05ResetFaultButton", "pi05ConnectionGate",
    "pi05LeaseGate", "pi05InitGate", "pi05ModeGate", "pi05DryRunGate",
    "pi05MetadataState", "pi05DryRunState", "pi05Latency", "pi05Chunk",
    "pi05ChunkRequestMode", "pi05ChunkFirstDelta", "pi05JointSpeedMetric", "pi05StepsPerChunkMetric",
    "pi05Executed", "pi05MetadataDetail", "pi05DryRunDetail",
    "pi05FaultBlock", "pi05Fault", "pi05CameraLimit", "pi05CameraMapHigh",
    "pi05CameraMapLeft", "pi05CameraMapRight", "pi05CameraAgeHigh",
    "pi05CameraAgeLeft", "pi05CameraAgeRight",
  ].forEach((id) => { dom[id] = document.getElementById(id); });
}

async function bootstrap() {
  await loadConfiguration();
  await pollState();
  scheduleStatePoll();
  await pollVoice();
  scheduleVoicePoll();
  await pollPi05();
  schedulePi05Poll();
}

function bindTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const targetId = tab.dataset.tab;
      document.querySelectorAll(".tab").forEach((item) => {
        const active = item === tab;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-selected", String(active));
      });
      document.querySelectorAll(".page").forEach((page) => {
        const active = page.id === targetId;
        page.classList.toggle("is-active", active);
        page.hidden = !active;
      });
      if (targetId !== "bodyPage") stopDrive();
    });
  });
}

function bindTakeover() {
  dom.takeoverToggle.checked = false;
  dom.takeoverToggle.addEventListener("change", async () => {
    if (state.takeoverPending) return;

    const requested = dom.takeoverToggle.checked;
    if (requested) {
      dom.takeoverToggle.checked = false;
      const accepted = await confirmAction({
        title: "接管机器人？",
        message: "接管后，本页面可以发送运动指令。请先清空机器人周围并确认没有其他控制端。",
        accept: "确认接管",
        tone: "warning",
      });
      if (!accepted) return;
      if (pi05BlocksOtherControls()) {
        toast("推理当前状态禁止变更控制租约", "error", 5000);
        return;
      }
    } else if (pi05BlocksOtherControls()) {
      dom.takeoverToggle.checked = true;
      toast("推理当前状态保持控制租约；请先使用停止按钮", "error", 5000);
      return;
    } else {
      await stopDrive();
    }

    state.takeoverPending = true;
    dom.takeoverToggle.disabled = true;
    try {
      const result = await postJson(
        API.takeover,
        { enabled: requested, client_id: state.clientId },
        30000,
        { lease: !requested },
      );
      if (requested) {
        const leaseId = result.lease_id ?? result.leaseId;
        if (!leaseId) throw new Error("后端未返回控制租约");
        state.leaseId = String(leaseId);
        state.remoteTakeover = false;
        applyTakeover(true);
        if (result.state) applyRobotState(result.state);
        startHeartbeat();
      } else {
        applyTakeover(false);
        clearLease();
      }
      toast(requested ? "控制权已接管" : "控制权已释放", "success");
    } catch (error) {
      applyTakeover(state.takeover);
      toast(requested ? error.message : `暂不能释放：${error.message}`, "error");
    } finally {
      state.takeoverPending = false;
      updateControlAvailability();
    }
  });
}

function bindLifecycleActions() {
  dom.initButton.addEventListener("click", () => runAction("init", {
    title: "初始化双臂？",
    message: "升降柱和双臂将产生较大范围运动。",
    detail: "确保机器人周围无人、无障碍物，且遥控/VR 未同时控制。",
    accept: "确认初始化",
  }));

  dom.deinitButton.addEventListener("click", () => runAction("deinit", {
    title: "反初始化？",
    message: "这不是断开连接：升降柱和双臂会执行实体回收运动。",
    detail: "请检查完整回收路径，确认底盘和机械臂周围无障碍物。",
    accept: "确认反初始化",
  }));

  dom.homeButton.addEventListener("click", () => {
    const speedScale = getMotionSpeedScale();
    const estimatedSeconds = 8 / speedScale;
    void runAction("home", {
      title: "移动到初始位姿？",
      message: `双臂、双夹爪与升降柱将同时移动，当前 ${formatSpeedScale(speedScale)}，手臂轨迹约 ${formatNumber(estimatedSeconds)} 秒。`,
      detail: "左/右臂 [0, 0, 0, -1.20, 0, 0, 0.98] rad\n夹爪 0.02 rad · 升降柱 0.40 m",
      accept: "确认移动",
    });
  });
}

function bindStopControls() {
  dom.stopButton.addEventListener("click", emergencyStop);
  dom.driveStopButton.addEventListener("click", () => {
    stopDrive();
    void sendChassis(0, 0, { quiet: false });
  });
}

function bindDriveControls() {
  document.querySelectorAll("[data-drive]").forEach((button) => {
    button.addEventListener("contextmenu", (event) => event.preventDefault());
    button.addEventListener("pointerdown", (event) => {
      if (!canControl()) return;
      event.preventDefault();
      button.setPointerCapture?.(event.pointerId);
      startDrive(button.dataset.drive, button);
    });
    ["pointerup", "pointercancel", "lostpointercapture", "pointerleave"].forEach((name) => {
      button.addEventListener(name, stopDrive);
    });
  });
}

function bindCameras() {
  document.querySelectorAll("[data-stream-group-toggle]").forEach((toggle) => {
    toggle.addEventListener("change", () => setStreamGroup(toggle.dataset.streamGroupToggle, toggle.checked));
  });
}

function bindVoice() {
  dom.voiceStartButton.addEventListener("click", () => {
    if (["synthesizing", "speaking"].includes(state.voiceState)) void cancelVoiceOutput();
    else if (state.voiceState === "listening") void finishVoiceInput();
    else void startVoiceSession();
  });
  dom.voiceCancelButton.addEventListener("click", () => { void cancelCurrentVoiceTask(); });
  dom.voiceAutoplay.addEventListener("change", () => {
    if (dom.voiceAutoplay.checked && dom.voiceAudio.src) void playVoiceAudio();
  });
  dom.voiceLanguage.addEventListener("change", () => {
    updateVoiceControls(state.voiceState, dom.voiceDetail.textContent);
  });
  dom.voiceMotionToggle.addEventListener("change", () => {
    void setVoiceMotionEnabled(dom.voiceMotionToggle.checked);
  });
  dom.voiceTextForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitVoiceText();
  });
}

function bindPi05() {
  const markEndpointDirty = () => {
    state.pi05EndpointDirty = true;
    state.pi05DryRunResult = null;
    updatePi05Controls();
  };
  dom.pi05Host.addEventListener("input", markEndpointDirty);
  dom.pi05Port.addEventListener("input", markEndpointDirty);
  dom.pi05Prompt.addEventListener("input", updatePi05Controls);
  dom.pi05JointSpeed.addEventListener("input", updatePi05Controls);
  dom.pi05ControlRate.addEventListener("input", updatePi05Controls);
  dom.pi05StepsPerChunk.addEventListener("input", updatePi05Controls);
  dom.pi05ProbeButton.addEventListener("click", () => { void probePi05(); });
  dom.pi05DryRunButton.addEventListener("click", () => { void dryRunPi05(); });
  dom.pi05StartButton.addEventListener("click", () => { void startPi05(); });
  dom.pi05EmergencyStopButton.addEventListener("click", () => { void emergencyStop(); });
  dom.pi05DisconnectButton.addEventListener("click", () => { void disconnectPi05(); });
  dom.pi05ReconnectButton.addEventListener("click", () => { void reconnectPi05(); });
  dom.pi05StopButton.addEventListener("click", () => { void stopPi05(); });
  dom.pi05ResetFaultButton.addEventListener("click", () => { void resetPi05Fault(); });
}

function pi05Phase() {
  if (!state.pi05StatusOnline) return "unknown";
  return String(state.pi05Status?.phase ?? "unknown").toLowerCase();
}

function pi05BlocksOtherControls() {
  return state.pi05StartPending || !PI05_CONTROL_UNLOCKED_PHASES.has(pi05Phase());
}

function hasCurrentControlLease() {
  return state.takeover && Boolean(state.leaseId) && !state.remoteTakeover && !state.takeoverPending;
}

function currentPi05Prompt() {
  return dom.pi05Prompt.value.trim();
}

function currentPi05Host() {
  const value = dom.pi05Host.value.trim();
  return value && value.length <= 255 && !/\s/.test(value) ? value : null;
}

function currentPi05Port() {
  const value = Number(dom.pi05Port.value);
  return Number.isInteger(value) && value >= 1 && value <= 65535 ? value : null;
}

function currentPi05ControlRate() {
  const value = Number(dom.pi05ControlRate.value);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function currentPi05JointSpeed() {
  const value = Number(dom.pi05JointSpeed.value);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function currentPi05StepsPerChunk() {
  const value = Number(dom.pi05StepsPerChunk.value);
  return Number.isInteger(value) && value >= 1 && value <= 50 ? value : null;
}

function pi05RemoteConnected(status = state.pi05Status) {
  if (!status || !state.pi05StatusOnline || state.pi05EndpointDirty) return false;
  for (const key of ["connected", "remote_connected", "policy_connected"]) {
    if (typeof status[key] === "boolean") return status[key];
  }
  return status.metadata_ok === true;
}

function pi05DryRunMatchesCurrent(status = state.pi05Status) {
  if (!status || pi05Phase() !== "dry_run_ready" || status.dry_run_ok !== true) return false;
  if (!pi05RemoteConnected(status)) return false;
  if (String(status.prompt ?? "") !== currentPi05Prompt()) return false;
  return status.required_confirmation === PI05_CONFIRMATION;
}

function setPi05Gate(element, ready, text) {
  element.dataset.ready = String(Boolean(ready));
  const label = element.querySelector("span");
  if (label) label.textContent = text;
}

function updatePi05Controls() {
  if (!dom.pi05ProbeButton) return;
  const phase = pi05Phase();
  const blocked = !PI05_CONTROL_UNLOCKED_PHASES.has(phase);
  const actionPending = state.pi05ActionPending || state.pi05StartPending;
  const configurationPhase = PI05_CONFIGURATION_PHASES.has(phase);
  const endpointReady = currentPi05Host() !== null && currentPi05Port() !== null;
  const remoteReady = pi05RemoteConnected();
  const leaseReady = hasCurrentControlLease();
  const initReady = state.initState === 2;
  const modeReady = state.controlModeName.toUpperCase() === "LOW_LEVEL";
  const promptReady = Boolean(currentPi05Prompt());
  const jointSpeedReady = currentPi05JointSpeed() !== null;
  const controlRateReady = currentPi05ControlRate() !== null;
  const stepsPerChunkReady = currentPi05StepsPerChunk() !== null;
  const dryRunReady = pi05DryRunMatchesCurrent();
  const metadataReady = remoteReady && state.pi05Status?.metadata_ok === true;

  setPi05Gate(
    dom.pi05ConnectionGate,
    metadataReady,
    metadataReady
      ? "远端已连接，metadata 已验证"
      : state.pi05EndpointDirty
        ? "远端配置已修改，需要重新连接"
        : "需要重新连接并验证 metadata",
  );

  setPi05Gate(
    dom.pi05LeaseGate,
    leaseReady,
    leaseReady ? "当前页面持有控制租约" : "需要当前页面接管控制",
  );
  setPi05Gate(
    dom.pi05InitGate,
    initReady,
    initReady ? "机器人已初始化（init_state = 2）" : "需要机器人 init_state = 2",
  );
  setPi05Gate(
    dom.pi05ModeGate,
    modeReady,
    modeReady ? "LOW_LEVEL 控制模式" : `当前模式：${state.controlModeName}`,
  );
  setPi05Gate(
    dom.pi05DryRunGate,
    dryRunReady,
    dryRunReady ? "当前连接的 dry-run 已通过，提示词一致" : "需要当前连接的 dry-run 通过且提示词未变化",
  );

  dom.pi05ProbeButton.disabled = actionPending || blocked || !configurationPhase || state.pi05EndpointDirty;
  dom.pi05DryRunButton.disabled = actionPending || blocked || !configurationPhase || !leaseReady || !metadataReady || !promptReady;
  dom.pi05StartButton.disabled = actionPending || blocked || !configurationPhase ||
    !leaseReady || !initReady || !modeReady || !metadataReady || !dryRunReady ||
    !jointSpeedReady || !controlRateReady || !stepsPerChunkReady;
  dom.pi05ReconnectButton.disabled = actionPending || state.pi05StopPending ||
    !configurationPhase || !endpointReady;
  dom.pi05DisconnectButton.disabled = actionPending || state.pi05StartPending || state.pi05StopPending;
  dom.pi05ResetFaultButton.disabled = state.pi05ActionPending || state.pi05StartPending || state.pi05StopPending || phase !== "fault";
  dom.pi05StopButton.disabled = state.pi05StopPending;
  const configurationLocked = actionPending || !configurationPhase;
  dom.pi05Host.disabled = configurationLocked;
  dom.pi05Port.disabled = configurationLocked;
  dom.pi05Prompt.disabled = configurationLocked;
  dom.pi05JointSpeed.disabled = configurationLocked;
  dom.pi05ControlRate.disabled = configurationLocked;
  dom.pi05StepsPerChunk.disabled = configurationLocked;
}

async function reconnectPi05() {
  const host = currentPi05Host();
  const port = currentPi05Port();
  if (!PI05_CONFIGURATION_PHASES.has(pi05Phase())) {
    return toast("当前推理状态已安全锁定；请先停止或清除故障", "error", 5000);
  }
  if (host === null || port === null) {
    return toast("请输入有效远端地址和 1～65535 的整数端口", "error", 4500);
  }
  if (state.pi05ActionPending || state.pi05StartPending || state.pi05StopPending) return;

  state.pi05ActionPending = true;
  state.pi05Metadata = null;
  state.pi05DryRunResult = null;
  updateControlAvailability();
  dom.pi05ReconnectButton.textContent = "正在连接…";
  try {
    const reconnectResult = await postJson(API.pi05Reconnect, { host, port }, 12000);
    state.pi05EndpointDirty = false;
    applyPi05Payload(reconnectResult);
    toast("远端推理已重新连接，metadata 验证通过", "success", 4200);
  } catch (error) {
    toast(`重新连接或 metadata 验证失败：${error.message}`, "error", 7000);
  } finally {
    state.pi05ActionPending = false;
    dom.pi05ReconnectButton.textContent = "重新连接";
    await pollPi05();
    updateControlAvailability();
  }
}

async function disconnectPi05() {
  if (state.pi05ActionPending || state.pi05StartPending || state.pi05StopPending) return;
  const accepted = await confirmAction({
    title: "断开远端推理连接？",
    message: "该操作只关闭远端推理连接，不会反初始化机器人。",
    detail: "如果真机推理仍在运行，连接中断会触发安全停止或锁存故障。断开后不会自动重连；需要操作员点击“重新连接”。",
    accept: "确认断开",
    tone: "warning",
  });
  if (!accepted) return;

  state.pi05ActionPending = true;
  updateControlAvailability();
  dom.pi05DisconnectButton.textContent = "正在断开…";
  try {
    const result = await postJson(API.pi05Disconnect, {}, 12000);
    state.pi05Metadata = null;
    state.pi05DryRunResult = null;
    applyPi05Payload(result);
    toast("远端推理连接已断开；机器人未反初始化", "success", 4200);
  } catch (error) {
    toast(`断开连接失败：${error.message}`, "error", 7000);
  } finally {
    state.pi05ActionPending = false;
    dom.pi05DisconnectButton.textContent = "断开连接";
    await pollPi05();
    updateControlAvailability();
  }
}

async function probePi05() {
  if (state.pi05ActionPending || state.pi05StartPending ||
      !PI05_CONFIGURATION_PHASES.has(pi05Phase()) || state.pi05EndpointDirty) return;
  state.pi05ActionPending = true;
  state.pi05DryRunResult = null;
  updateControlAvailability();
  dom.pi05ProbeButton.textContent = "正在检查…";
  try {
    const result = await postJson(API.pi05Probe, {}, 20000);
    applyPi05Payload(result);
    toast("推理 healthz 与 metadata 验证通过", "success", 3500);
  } catch (error) {
    toast(`推理探针失败：${error.message}`, "error", 6500);
  } finally {
    state.pi05ActionPending = false;
    dom.pi05ProbeButton.textContent = "重新验证 metadata";
    await pollPi05();
    updateControlAvailability();
  }
}

async function dryRunPi05() {
  const prompt = currentPi05Prompt();
  if (!hasCurrentControlLease()) return toast("dry-run 需要当前页面持有控制租约", "error", 4500);
  if (!prompt) return toast("请输入任务提示词", "error");
  if (!pi05RemoteConnected() || state.pi05Status?.metadata_ok !== true) {
    return toast("请先重新连接并完成 healthz + metadata 探针", "error", 4500);
  }
  if (state.pi05ActionPending || state.pi05StartPending ||
      !PI05_CONFIGURATION_PHASES.has(pi05Phase())) return;

  state.pi05ActionPending = true;
  state.pi05DryRunResult = null;
  updateControlAvailability();
  dom.pi05DryRunButton.textContent = "推理验证中…";
  try {
    const result = await postJson(API.pi05DryRun, { prompt }, 30000, { lease: true });
    state.pi05DryRunResult = result;
    toast("dry-run 通过：未发送任何电机动作", "success", 4000);
  } catch (error) {
    toast(`dry-run 失败并已锁存：${error.message}`, "error", 7000);
  } finally {
    state.pi05ActionPending = false;
    dom.pi05DryRunButton.textContent = "执行 dry-run";
    await pollPi05();
    renderPi05Status();
    updateControlAvailability();
  }
}

async function startPi05() {
  const prompt = currentPi05Prompt();
  const jointSpeed = currentPi05JointSpeed();
  const controlRate = currentPi05ControlRate();
  const stepsPerChunk = currentPi05StepsPerChunk();
  if (!hasCurrentControlLease()) return toast("真机启动需要当前页面持有控制租约", "error", 5000);
  if (state.initState !== 2) return toast("真机启动要求 init_state = 2", "error", 5000);
  if (state.controlModeName.toUpperCase() !== "LOW_LEVEL") return toast("真机启动要求 LOW_LEVEL 控制模式", "error", 5000);
  if (!pi05RemoteConnected() || state.pi05Status?.metadata_ok !== true) {
    return toast("真机启动要求远端已连接且 metadata 已验证", "error", 5000);
  }
  if (!pi05DryRunMatchesCurrent()) return toast("请用相同提示词完成当前连接的 dry-run", "error", 5500);
  if (!prompt || jointSpeed === null || controlRate === null || stepsPerChunk === null) {
    return toast("请检查提示词、关节速度和发送频率（必须大于 0），以及每个 Chunk 执行步数（1～50）", "error", 5500);
  }

  const maxArmStepRad = jointSpeed * Math.PI / 180 / controlRate;

  const accepted = await confirmAction({
    title: "启动推理真机执行？",
    message: "确认后将向真实机器人连续发送动作。双臂、双夹爪和升降柱会产生实体运动。",
    detail: `关节速度限幅：${jointSpeed} deg/s\n每周期双臂目标最大变化：${maxArmStepRad.toFixed(5)} rad（基于上一条成功下发目标，不基于反馈）\n发送频率：${controlRate} Hz\n每个 Chunk 执行：${stepsPerChunk} / 50 步\n请求时序：执行完 N 步后重新读取最新状态和图片，再同步请求下一包；推理期间保持上一目标\n连续执行：不设总 Chunk 或总执行步数上限\n退出：服务端没有 is_success；必须由操作员手动停止，或在发生故障时退出\n腰和头：每步保持最新观测位置\n底盘：线速度与角速度强制为 0，禁止移动\n安全：请确保实体急停始终可达，并安排操作员全程监护`,
    accept: "我已确认，启动真机",
    tone: "danger",
  });
  if (!accepted) return;

  if (prompt !== currentPi05Prompt() || jointSpeed !== currentPi05JointSpeed() ||
      controlRate !== currentPi05ControlRate() ||
      stepsPerChunk !== currentPi05StepsPerChunk() ||
      !hasCurrentControlLease() || state.initState !== 2 ||
      state.controlModeName.toUpperCase() !== "LOW_LEVEL" || !pi05RemoteConnected() ||
      state.pi05Status?.metadata_ok !== true || !pi05DryRunMatchesCurrent()) {
    toast("确认期间启动条件已变化，请重新检查", "error", 5500);
    updatePi05Controls();
    return;
  }

  state.pi05StartPending = true;
  stopDrive();
  updateControlAvailability();
  dom.pi05StartButton.textContent = "正在启动…";
  try {
    const result = await postJson(
      API.pi05Start,
      {
        prompt,
        joint_speed_deg_s: jointSpeed,
        control_rate_hz: controlRate,
        steps_per_chunk: stepsPerChunk,
        confirmation: PI05_CONFIRMATION,
      },
      10000,
      { lease: true },
    );
    applyPi05Payload(result);
    toast("推理真机执行已启动，请持续监护", "success", 4500);
  } catch (error) {
    toast(`推理启动失败：${error.message}`, "error", 7000);
  } finally {
    state.pi05StartPending = false;
    dom.pi05StartButton.textContent = "确认并启动真机";
    await pollPi05();
    updateControlAvailability();
  }
}

async function stopPi05({ quiet = false } = {}) {
  if (state.pi05StopPending) return false;
  state.pi05StopPending = true;
  updatePi05Controls();
  dom.pi05StopButton.textContent = "正在停止…";
  try {
    const result = await postJson(API.pi05Stop, {}, 9000);
    applyPi05Payload(result);
    if (!quiet) toast("推理停止请求已完成", "success", 3000);
    return true;
  } catch (error) {
    if (!quiet) toast(`推理停止失败：${error.message}`, "error", 7000);
    return false;
  } finally {
    state.pi05StopPending = false;
    dom.pi05StopButton.textContent = "■ 停止推理";
    await pollPi05();
    updateControlAvailability();
  }
}

async function resetPi05Fault() {
  if (pi05Phase() !== "fault" || state.pi05ActionPending || state.pi05StartPending) return;
  state.pi05ActionPending = true;
  updateControlAvailability();
  dom.pi05ResetFaultButton.textContent = "正在清除…";
  try {
    const result = await postJson(API.pi05ResetFault, {}, 7000);
    state.pi05Metadata = null;
    state.pi05DryRunResult = null;
    applyPi05Payload(result);
    toast("推理故障已清除；请重新连接、验证 metadata 并完成 dry-run", "success", 5000);
  } catch (error) {
    toast(`故障清除失败：${error.message}`, "error", 6500);
  } finally {
    state.pi05ActionPending = false;
    dom.pi05ResetFaultButton.textContent = "清除锁存故障";
    await pollPi05();
    updateControlAvailability();
  }
}

async function pollPi05() {
  try {
    const result = await getJson(API.pi05Status, 1800);
    state.pi05StatusOnline = true;
    applyPi05Payload(result);
  } catch (_) {
    state.pi05StatusOnline = false;
    renderPi05Status();
    updateControlAvailability();
  }
}

function schedulePi05Poll() {
  window.clearTimeout(state.pi05Timer);
  const phase = pi05Phase();
  const delay = ["probing", "running", "stopping"].includes(phase) ? 250 : 1000;
  state.pi05Timer = window.setTimeout(async () => {
    await pollPi05();
    schedulePi05Poll();
  }, delay);
}

function applyPi05Payload(payload) {
  if (!payload || typeof payload !== "object") return;
  if (payload.metadata && typeof payload.metadata === "object" && !Array.isArray(payload.metadata)) {
    state.pi05Metadata = payload.metadata;
  }
  const nestedStatus = payload.status && typeof payload.status === "object" && !Array.isArray(payload.status)
    ? payload.status
    : null;
  const status = nestedStatus ?? (typeof payload.phase === "string" ? payload : null);
  if (status) {
    state.pi05StatusOnline = true;
    applyPi05Status(status);
  }
}

function applyPi05Status(status) {
  const previousPhase = pi05Phase();
  state.pi05Status = { ...status };
  const nextPhase = pi05Phase();
  if (PI05_CONTROL_UNLOCKED_PHASES.has(previousPhase) &&
      !PI05_CONTROL_UNLOCKED_PHASES.has(nextPhase)) stopDrive();
  renderPi05Status();
  updateControlAvailability();
  updateVoiceControls(state.voiceState, dom.voiceDetail.textContent);
}

function pi05PhaseLabel(phase) {
  return ({
    idle: "空闲 / 等待任务",
    probing: "正在连接验证",
    dry_run_ready: "Dry-run 已就绪",
    running: "真机执行中",
    stopping: "正在停止",
    fault: "故障已锁存",
    unknown: "状态未知",
  })[phase] ?? "状态未知";
}

function formatPi05Milliseconds(value) {
  if (value == null || value === "") return "--";
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? `${number.toFixed(number < 10 ? 1 : 0)} ms` : "--";
}

function userFacingInferenceText(value) {
  return String(value).replace(/pi0\.5/gi, "推理");
}

function formatPi05Fault(fault) {
  if (fault == null || fault === "") return "无";
  if (typeof fault === "string") return userFacingInferenceText(fault);
  try { return userFacingInferenceText(JSON.stringify(fault)); } catch (_) { return userFacingInferenceText(fault); }
}

function pi05EndpointParts(status) {
  const directHost = status.host ?? status.server_host ?? status.policy_host;
  const directPort = Number(status.port ?? status.server_port ?? status.policy_port);
  if (typeof directHost === "string" && directHost.trim() &&
      Number.isInteger(directPort) && directPort >= 1 && directPort <= 65535) {
    return { host: directHost.trim(), port: directPort };
  }
  const endpoint = String(status.endpoint ?? "").trim();
  if (!endpoint) return null;
  try {
    const parsed = new URL(endpoint.includes("://") ? endpoint : `ws://${endpoint}`);
    const port = Number(parsed.port);
    return parsed.hostname && Number.isInteger(port) && port >= 1 && port <= 65535
      ? { host: parsed.hostname, port }
      : null;
  } catch (_) {
    return null;
  }
}

function renderPi05Status() {
  if (!dom.pi05PhaseBadge) return;
  const status = state.pi05Status ?? {};
  const phase = pi05Phase();
  const knownPhase = ["idle", "probing", "dry_run_ready", "running", "stopping", "fault"].includes(phase)
    ? phase
    : "unknown";
  dom.pi05PhaseBadge.dataset.phase = knownPhase;
  dom.pi05PhaseText.textContent = pi05PhaseLabel(knownPhase);
  dom.pi05PollState.textContent = state.pi05StatusOnline ? "状态在线" : "状态接口不可达";
  dom.pi05PollState.classList.toggle("is-online", state.pi05StatusOnline);
  dom.pi05PollState.classList.toggle("is-offline", !state.pi05StatusOnline);
  const endpointText = String(status.endpoint ?? "192.168.1.154:9973");
  dom.pi05Endpoint.textContent = endpointText;
  if (!state.pi05EndpointDirty) {
    const endpoint = pi05EndpointParts(status);
    if (endpoint) {
      if (document.activeElement !== dom.pi05Host) dom.pi05Host.value = endpoint.host;
      if (document.activeElement !== dom.pi05Port) dom.pi05Port.value = String(endpoint.port);
    }
  }

  const metadataOk = status.metadata_ok === true;
  const dryRunOk = status.dry_run_ok === true;
  dom.pi05MetadataState.textContent = metadataOk ? "已验证" : "未验证";
  const dryAge = status.dry_run_age_ms == null ? NaN : Number(status.dry_run_age_ms);
  dom.pi05DryRunState.textContent = dryRunOk
    ? `已通过 · ${Number.isFinite(dryAge) ? formatPi05Milliseconds(dryAge) + " 前" : "有效"}`
    : "未就绪";
  dom.pi05Latency.textContent = formatPi05Milliseconds(status.inference_latency_ms);
  const chunkLength = status.chunk_length == null ? NaN : Number(status.chunk_length);
  dom.pi05Chunk.textContent = Number.isFinite(chunkLength) ? `${chunkLength} 步` : "--";
  dom.pi05ChunkRequestMode.textContent = status.chunk_request_mode === "after_chunk_sync"
    ? "包尾同步"
    : "--";
  const firstDelta = status.last_chunk_first_arm_delta_from_feedback_rad == null
    ? NaN
    : Number(status.last_chunk_first_arm_delta_from_feedback_rad);
  dom.pi05ChunkFirstDelta.textContent = Number.isFinite(firstDelta)
    ? `${firstDelta.toFixed(4)} rad`
    : "--";
  const jointSpeed = Number(status.joint_speed_deg_s);
  const maxArmStep = Number(status.max_arm_step_rad);
  dom.pi05JointSpeedMetric.textContent = Number.isFinite(jointSpeed)
    ? `${jointSpeed.toFixed(jointSpeed % 1 ? 1 : 0)} deg/s${Number.isFinite(maxArmStep) ? ` · ${maxArmStep.toFixed(4)} rad/步` : ""}`
    : "--";
  const executed = Number(status.executed_steps);
  const stepsPerChunk = Number(status.steps_per_chunk);
  dom.pi05StepsPerChunkMetric.textContent = `${Number.isFinite(stepsPerChunk) ? stepsPerChunk : 30} / 50 步`;
  dom.pi05Executed.textContent = `${Number.isFinite(executed) ? executed : 0} 步`;

  if (state.pi05Metadata) {
    dom.pi05MetadataDetail.textContent = userFacingInferenceText(JSON.stringify(state.pi05Metadata, null, 2));
  } else if (metadataOk) {
    dom.pi05MetadataDetail.textContent = "zerith_h1_pro · state 23 · action 23 · policy 17\n连续输入夹爪 · 二值输出夹爪 · no-status";
  } else {
    dom.pi05MetadataDetail.textContent = "尚未执行 metadata 探针";
  }

  if (state.pi05DryRunResult) {
    dom.pi05DryRunDetail.textContent = userFacingInferenceText(JSON.stringify(state.pi05DryRunResult, null, 2));
  } else if (dryRunOk) {
    dom.pi05DryRunDetail.textContent = `prompt: ${String(status.prompt ?? "--")}\nchunk: ${Number.isFinite(chunkLength) ? chunkLength : "--"} × 23\nlatency: ${formatPi05Milliseconds(status.inference_latency_ms)}`;
  } else {
    dom.pi05DryRunDetail.textContent = "尚未执行 dry-run";
  }

  const fault = formatPi05Fault(status.fault);
  const hasFault = status.fault != null && status.fault !== "";
  dom.pi05FaultBlock.dataset.fault = String(hasFault);
  dom.pi05Fault.textContent = fault;

  dom.pi05CameraLimit.textContent = "最近观测帧龄";
  const cameraMapping = status.camera_mapping && typeof status.camera_mapping === "object"
    ? status.camera_mapping
    : {};
  const cameraAges = status.camera_ages_ms && typeof status.camera_ages_ms === "object"
    ? status.camera_ages_ms
    : {};
  PI05_CAMERA_UI.forEach(({ wire, service, client, mapId, ageId }) => {
    const mappedService = String(cameraMapping[wire] ?? service);
    dom[mapId].textContent = `${mappedService} / ${client}`;
    const age = cameraAges[wire] == null ? NaN : Number(cameraAges[wire]);
    dom[ageId].textContent = formatPi05Milliseconds(age);
    dom[ageId].classList.remove("is-stale");
  });
  updatePi05Controls();
}

async function setVoiceMotionEnabled(enabled) {
  if (state.voiceMotionPending) return;
  dom.voiceMotionToggle.checked = state.voiceMotionEnabled;
  if (pi05BlocksOtherControls()) {
    toast("推理正在运行、停止、故障锁存或状态未知，语音运动控制已禁用", "error", 5000);
    return;
  }
  if (enabled) {
    if (!canControl() || state.initState !== 2) {
      toast("请先接管机器人并完成初始化", "error", 4500);
      return;
    }
    const accepted = await confirmAction({
      title: "开启对话运动控制？",
      message: "开启后，明确的语音或键盘文字指令可以直接让机器人移动或挥手。",
      detail: "请清空机器人周围，确认实体急停可达。底盘只执行低速固定时长动作；转身角度未经里程计标定。",
      accept: "确认开启",
      tone: "warning",
    });
    if (!accepted) return;
    if (!canControl() || state.initState !== 2 || pi05BlocksOtherControls()) {
      toast("确认期间机器人控制状态已变化", "error", 5000);
      return;
    }
  }

  state.voiceMotionPending = true;
  updateControlAvailability();
  try {
    const result = await postJson(
      API.voiceMotion,
      { enabled },
      7000,
      { lease: true },
    );
    applyVoiceMotionState(result);
    toast(enabled ? "语音 / 文字运动控制已开启" : "对话运动控制已关闭并停止动作", "success", 3200);
  } catch (error) {
    dom.voiceMotionToggle.checked = state.voiceMotionEnabled;
    toast(`语音运动控制切换失败：${error.message}`, "error", 5500);
  } finally {
    state.voiceMotionPending = false;
    updateControlAvailability();
  }
}

async function startVoiceSession() {
  if (pi05BlocksOtherControls()) return toast("推理当前状态禁止启动语音任务", "error", 4500);
  if (!state.voiceOnline || state.voiceStartPending) return;
  const language = dom.voiceLanguage.value === "en" ? "en" : "zh";
  await startRobotMicrophoneSession(language);
}

async function startRobotMicrophoneSession(language) {
  state.voiceStartPending = true;
  updateVoiceControls("starting", "正在启动机器人麦克风…");
  try {
    const result = await postJson(API.voiceStart, { language }, 5000);
    toast(result.message ?? "请听提示音后开始说话", "success");
    await pollVoice();
  } catch (error) {
    toast(`无法启动语音对话：${error.message}`, "error", 5000);
    await pollVoice();
  } finally {
    state.voiceStartPending = false;
  }
}

async function finishVoiceInput() {
  if (state.voiceBrowserListening) {
    state.voiceBrowserFinishing = true;
    stopChineseAudioCapture();
    if (state.voiceSocket?.readyState === WebSocket.OPEN) {
      state.voiceSocket.send(JSON.stringify({ type: "finish" }));
      updateVoiceControls("transcribing", "句末已提交，正在复核…");
    }
    return;
  }
  if (!state.voiceOnline || state.voiceStartPending || state.voiceState !== "listening") return;
  state.voiceStartPending = true;
  updateVoiceControls("transcribing", "正在结束录音…");
  try {
    const result = await postJson(API.voiceFinishInput, {}, 5000);
    toast(result.message ?? "已结束输入，正在识别", "success", 1800);
    await pollVoice();
  } catch (error) {
    toast(`无法结束输入：${error.message}`, "error", 5000);
    await pollVoice();
  } finally {
    state.voiceStartPending = false;
  }
}

async function startChineseVoiceInput() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    toast("当前 HTTP 地址不能访问浏览器麦克风，已改用机器人麦克风；HTTPS/localhost 下可实时出字。", "error", 7000);
    await startRobotMicrophoneSession("zh");
    return;
  }
  state.voiceStartPending = true;
  state.voiceBrowserFinishing = false;
  updateVoiceControls("starting", "正在申请浏览器麦克风…");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
    const context = new AudioContext({ latencyHint: "interactive" });
    await context.audioWorklet.addModule("/static/pcm-worklet.js");
    const source = context.createMediaStreamSource(stream);
    const worklet = new AudioWorkletNode(context, "pcm16-downsampler", {
      numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
    });
    const sink = context.createGain();
    sink.gain.value = 0;
    source.connect(worklet).connect(sink).connect(context.destination);
    state.voiceMediaStream = stream;
    state.voiceAudioContext = context;
    state.voiceAudioSource = source;
    state.voiceAudioWorklet = worklet;
    state.voiceAudioSink = sink;
    worklet.port.onmessage = (event) => {
      if (state.voiceSocket?.readyState === WebSocket.OPEN && state.voiceBrowserListening) {
        state.voiceSocket.send(event.data);
      }
    };
    state.voiceBrowserListening = true;
    state.voiceReconnectDelay = 300;
    openChineseVoiceSocket();
    renderLiveVoiceText("");
    updateVoiceControls("listening", "中文实时识别：请开始说话…");
  } catch (error) {
    state.voiceBrowserListening = false;
    stopChineseAudioCapture();
    toast(`无法启动中文实时识别：${error.message}`, "error", 6000);
    await pollVoice();
  } finally {
    state.voiceStartPending = false;
  }
}

function openChineseVoiceSocket() {
  if (!state.voiceBrowserListening || state.voiceBrowserFinishing) return;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}${API.voiceAsrWs}`);
  socket.binaryType = "arraybuffer";
  state.voiceSocket = socket;
  socket.addEventListener("open", () => {
    if (socket !== state.voiceSocket) return;
    state.voiceReconnectDelay = 300;
    socket.send(JSON.stringify({ type: "start", language: "zh", sample_rate: 16000, format: "pcm_s16le" }));
  });
  socket.addEventListener("message", (event) => {
    if (socket !== state.voiceSocket || typeof event.data !== "string") return;
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === "partial") {
      renderLiveVoiceText(String(message.text ?? ""));
      updateVoiceControls("listening", `实时识别：${message.text || "…"}`);
    } else if (message.type === "final") {
      renderLiveVoiceText(String(message.text ?? ""), true);
      state.voiceBrowserListening = false;
      state.voiceBrowserFinishing = false;
      stopChineseAudioCapture();
      updateVoiceControls("starting", "最终文本已提交，小达正在处理…");
      socket.close(1000, "final received");
      window.setTimeout(pollVoice, 100);
    } else if (message.type === "error") {
      toast(`中文识别失败：${message.error}`, "error", 6000);
    }
  });
  socket.addEventListener("close", () => {
    if (socket !== state.voiceSocket) return;
    state.voiceSocket = null;
    if (state.voiceBrowserListening && !state.voiceBrowserFinishing) scheduleChineseVoiceReconnect();
  });
  socket.addEventListener("error", () => socket.close());
}

function scheduleChineseVoiceReconnect() {
  window.clearTimeout(state.voiceReconnectTimer);
  updateVoiceControls("listening", "实时识别连接中断，正在自动重连…");
  state.voiceReconnectTimer = window.setTimeout(openChineseVoiceSocket, state.voiceReconnectDelay);
  state.voiceReconnectDelay = Math.min(3000, state.voiceReconnectDelay * 1.7);
}

function stopChineseAudioCapture() {
  state.voiceMediaStream?.getTracks().forEach((track) => track.stop());
  state.voiceAudioSource?.disconnect();
  state.voiceAudioWorklet?.disconnect();
  state.voiceAudioSink?.disconnect();
  if (state.voiceAudioContext && state.voiceAudioContext.state !== "closed") {
    void state.voiceAudioContext.close();
  }
  state.voiceMediaStream = null;
  state.voiceAudioContext = null;
  state.voiceAudioSource = null;
  state.voiceAudioWorklet = null;
  state.voiceAudioSink = null;
}

function closeChineseVoiceSocket() {
  window.clearTimeout(state.voiceReconnectTimer);
  state.voiceReconnectTimer = null;
  const socket = state.voiceSocket;
  state.voiceSocket = null;
  if (socket?.readyState <= WebSocket.OPEN) {
    try { socket.close(1000, "client closed"); } catch (_) { /* connecting socket */ }
  }
}

async function cancelVoiceOutput() {
  try {
    await postJson(API.voiceCancel, {}, 2000);
    toast("已停止当前中文播报", "success", 1800);
    window.setTimeout(pollVoice, 100);
  } catch (error) {
    toast(`停止播报失败：${error.message}`, "error", 5000);
  }
}

async function cancelCurrentVoiceTask() {
  if (state.voiceBrowserListening || state.voiceBrowserFinishing) {
    state.voiceBrowserListening = false;
    state.voiceBrowserFinishing = false;
    if (state.voiceSocket?.readyState === WebSocket.OPEN) {
      state.voiceSocket.send(JSON.stringify({ type: "cancel" }));
    }
    stopChineseAudioCapture();
    closeChineseVoiceSocket();
    renderLiveVoiceText("已取消录音", true);
  }
  try {
    await postJson(API.voiceCancel, {}, 2000);
    toast("已取消当前语音任务", "success", 1800);
    await pollVoice();
  } catch (error) {
    toast(`取消失败：${error.message}`, "error", 5000);
  }
}

function renderLiveVoiceText(text, final = false) {
  let item = dom.voiceTranscript.querySelector("[data-live-asr]");
  if (!item) {
    dom.voiceTranscript.innerHTML = "";
    item = document.createElement("div");
    item.className = "voice-message voice-message--user";
    item.dataset.liveAsr = "true";
    const label = document.createElement("span");
    label.className = "voice-message-role";
    label.textContent = "你 · 实时";
    const content = document.createElement("p");
    item.append(label, content);
    dom.voiceTranscript.append(item);
  }
  item.querySelector("p").textContent = text || "正在聆听…";
  item.classList.toggle("is-final", final);
}

async function submitVoiceText() {
  const text = dom.voiceTextInput.value.trim();
  if (pi05BlocksOtherControls()) return toast("推理当前状态禁止发送语音或文字任务", "error", 4500);
  if (!text || !state.voiceOnline || state.voiceTextPending || state.voiceState !== "idle") return;
  state.voiceTextPending = true;
  updateVoiceControls("starting", "正在提交键盘输入…");
  try {
    const language = dom.voiceLanguage.value === "en" ? "en" : "zh";
    const result = await postJson(API.voiceText, { text, language }, 5000);
    dom.voiceTextInput.value = "";
    toast(result.message ?? "文字已发送，正在处理", "success", 2200);
    await pollVoice();
  } catch (error) {
    toast(`文字发送失败：${error.message}`, "error", 5000);
    await pollVoice();
  } finally {
    state.voiceTextPending = false;
    updateVoiceControls(state.voiceState, dom.voiceDetail.textContent);
  }
}

async function pollVoice() {
  try {
    const data = await getJson(API.voiceStatus, 1800);
    state.voiceOnline = Boolean(data.available);
    applyVoiceState(data);
  } catch (_) {
    state.voiceOnline = false;
    applyVoiceState({
      state: "offline",
      detail: "小达常驻服务未连接",
      messages: [],
      sequence: null,
      session_id: null,
    });
  }
}

function scheduleVoicePoll() {
  window.clearTimeout(state.voiceTimer);
  state.voiceTimer = window.setTimeout(async () => {
    await pollVoice();
    scheduleVoicePoll();
  }, ["starting", "listening", "transcribing", "thinking", "acting", "synthesizing", "speaking"].includes(state.voiceState) ? 300 : 1200);
}

function applyVoiceState(data) {
  const voiceState = String(data.state ?? "offline");
  const detail = String(data.detail ?? "");
  if (!state.voiceBrowserListening) updateVoiceControls(voiceState, detail);
  applyVoiceMotionState(data.motion);

  const sessionId = data.session_id ?? null;
  if (state.voiceSessionId !== sessionId) {
    state.voiceSessionId = sessionId;
    state.voiceSequence = undefined;
    state.latestVoiceAudioId = null;
    dom.voiceAudio.pause();
    dom.voiceAudio.removeAttribute("src");
    dom.voiceAudio.load();
  }

  if (data.sequence == null || state.voiceSequence !== data.sequence) {
    state.voiceSequence = data.sequence ?? null;
    renderVoiceMessages(Array.isArray(data.messages) ? data.messages : []);
  }
}

function applyVoiceMotionState(motion) {
  if (!motion || motion.available === false) {
    state.voiceMotionEnabled = false;
    state.voiceMotionReady = false;
    dom.voiceMotionToggle.checked = false;
    dom.voiceMotionHint.textContent = "对话运动桥接不可用";
    updateControlAvailability();
    return;
  }
  state.voiceMotionEnabled = Boolean(motion.enabled);
  state.voiceMotionReady = Boolean(motion.ready);
  dom.voiceMotionToggle.checked = state.voiceMotionEnabled;
  const actionNames = {
    forward: "前进", backward: "后退", turn_left: "左转", turn_right: "右转",
    turn_around: "转身", wave: "挥手", handshake: "握手", stop: "停止",
  };
  if (motion.active_action) {
    dom.voiceMotionHint.textContent = `正在执行：${actionNames[motion.active_action] ?? motion.active_action}`;
  } else if (motion.last_error) {
    dom.voiceMotionHint.textContent = `上次动作失败：${motion.last_error}`;
  } else if (state.voiceMotionEnabled) {
    dom.voiceMotionHint.textContent = "已开启：支持前进、后退、左右转、转身、挥手、握手、停止";
  } else {
    dom.voiceMotionHint.textContent = state.voiceMotionReady
      ? "已就绪，默认关闭"
      : "需先接管并初始化机器人";
  }
  updateControlAvailability();
}

function updateVoiceControls(voiceState, detail) {
  state.voiceState = voiceState;
  const labels = {
    starting: "正在启动",
    idle: "网页待命",
    listening: "正在聆听",
    transcribing: "正在识别",
    thinking: "正在思考",
    acting: "正在执行动作",
    synthesizing: "正在合成语音",
    speaking: "正在播报",
    error: "服务异常",
    stopped: "已停止",
    offline: "服务离线",
  };
  dom.voiceStateText.textContent = labels[voiceState] ?? voiceState;
  dom.voiceDetail.textContent = detail || labels[voiceState] || "--";
  dom.voiceStateBadge.dataset.state = voiceState;
  dom.voiceOrb.dataset.state = voiceState;
  const pi05Blocked = pi05BlocksOtherControls();
  const canStart = !pi05Blocked && state.voiceOnline && voiceState === "idle" && !state.voiceStartPending;
  const canFinish = !pi05Blocked && state.voiceOnline && voiceState === "listening" && !state.voiceStartPending;
  const canCancel = state.voiceOnline && ["synthesizing", "speaking"].includes(voiceState);
  dom.voiceStartButton.disabled = !(canStart || canFinish || canCancel);
  dom.voiceStartButton.classList.toggle("button--recording", canFinish);
  dom.voiceStartText.textContent = canCancel ? "停止播报" : canFinish ? "结束输入" : voiceState === "starting" ? "正在启动…" : "录入一句";
  dom.voiceCancelButton.disabled = !state.voiceOnline || !["starting", "listening", "transcribing", "thinking", "synthesizing", "speaking"].includes(voiceState);
  const canSendText = !pi05Blocked && state.voiceOnline && voiceState === "idle" && !state.voiceStartPending && !state.voiceTextPending;
  dom.voiceLanguage.disabled = pi05Blocked || voiceState !== "idle" || state.voiceStartPending || state.voiceTextPending;
  dom.voiceTextInput.disabled = !canSendText;
  dom.voiceTextSend.disabled = !canSendText;
  dom.voiceTextHint.textContent = pi05Blocked
    ? "推理当前状态已禁用语音与文字任务"
    : !state.voiceOnline
    ? "语音服务未连接"
    : canSendText
      ? "按 Enter 发送；运动指令仍受接管、初始化和运动控制开关保护"
      : "小达正在处理上一条输入…";
  if (dom.voiceInputHint) {
    dom.voiceInputHint.textContent = dom.voiceLanguage.value === "zh"
      ? "中文默认使用机器人独立麦克风，通过本地 Paraformer 与 Qwen3-ASR 双阶段识别。"
      : "English 保持原链路：使用机器人本体麦克风，一次录入一句。";
  }
}

function renderVoiceMessages(messages) {
  dom.voiceTranscript.innerHTML = "";
  if (!messages.length) {
    const empty = document.createElement("div");
    empty.className = "voice-empty";
    empty.textContent = state.voiceOnline ? "尚未开始对话" : "语音服务未连接";
    dom.voiceTranscript.append(empty);
    return;
  }

  let newestAudioId = null;
  messages.forEach((message) => {
    const role = ["user", "assistant", "system"].includes(message.role) ? message.role : "system";
    const item = document.createElement("div");
    item.className = `voice-message voice-message--${role}`;
    const label = document.createElement("span");
    label.className = "voice-message-role";
    label.textContent = role === "user" ? "你" : role === "assistant" ? "小达" : "系统";
    const text = document.createElement("p");
    text.textContent = String(message.text ?? "");
    item.append(label, text);
    if (message.audio_id != null) {
      newestAudioId = message.audio_id;
      const replay = document.createElement("button");
      replay.type = "button";
      replay.className = "voice-replay";
      replay.textContent = "播放";
      replay.addEventListener("click", () => setVoiceAudio(message.audio_id, true, true));
      item.append(replay);
    }
    dom.voiceTranscript.append(item);
  });
  dom.voiceTranscript.scrollTop = dom.voiceTranscript.scrollHeight;
  if (newestAudioId != null) setVoiceAudio(newestAudioId, dom.voiceAutoplay.checked);
}

function setVoiceAudio(audioId, shouldPlay = false, forcePlay = false) {
  const changed = String(state.latestVoiceAudioId) !== String(audioId);
  if (changed) {
    state.latestVoiceAudioId = audioId;
    dom.voiceAudio.src = API.voiceAudio(audioId);
    dom.voiceAudio.load();
  }
  if (shouldPlay && (changed || forcePlay)) void playVoiceAudio();
}

async function playVoiceAudio() {
  try {
    await dom.voiceAudio.play();
  } catch (_) {
    toast("浏览器阻止了自动播放，请点击音频播放器的播放键", "error", 4200);
  }
}

function bindModal() {
  dom.confirmCancel.addEventListener("click", () => resolveModal(false));
  dom.confirmAccept.addEventListener("click", () => resolveModal(true));
  dom.confirmModal.addEventListener("click", (event) => {
    if (event.target === dom.confirmModal) resolveModal(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !dom.confirmModal.hidden) resolveModal(false);
  });
}

async function loadConfiguration() {
  try {
    const config = await getJson(API.config, 5000);
    renderMotorControls(config.motors);
    renderSpeedControls(config.chassis);
    renderMotionSpeedControl(config.motion_speed);
    state.configLoaded = true;
  } catch (error) {
    setPlaceholders("无法读取 SDK 限位，控制已锁定");
    toast(`配置加载失败：${error.message}`, "error", 6000);
  }
  updateControlAvailability();
}

function renderMotorControls(motors) {
  if (!Array.isArray(motors)) throw new Error("/api/config 未返回 motors 数组");

  Object.values(GROUP_TARGETS).forEach((id) => { dom[id].innerHTML = ""; });
  dom.leftGripperControl.innerHTML = "";
  dom.rightGripperControl.innerHTML = "";
  state.motorElements.clear();

  const validMotors = motors.filter(isValidMotorConfig);
  validMotors.forEach((motor) => {
    const normalized = normalizeMotor(motor);
    const control = createMotorControl(normalized);

    if (isGripper(normalized)) {
      const target = normalized.id === 14 ? dom.leftGripperControl : dom.rightGripperControl;
      const wrapper = document.createElement("div");
      wrapper.className = "gripper-control";
      wrapper.append(control);
      target.append(wrapper);
      return;
    }

    const group = GROUP_ALIASES[normalized.group];
    const targetId = GROUP_TARGETS[group];
    if (targetId) dom[targetId].append(control);
  });

  Object.values(GROUP_TARGETS).forEach((id) => {
    if (!dom[id].children.length) dom[id].innerHTML = '<div class="panel-placeholder">后端未提供可控电机</div>';
  });
}

function isValidMotorConfig(motor) {
  return motor && Number.isInteger(Number(motor.id)) &&
    Number.isFinite(Number(motor.min)) && Number.isFinite(Number(motor.max)) &&
    Number(motor.max) > Number(motor.min);
}

function normalizeMotor(motor) {
  const min = Number(motor.min);
  const max = Number(motor.max);
  const proposedStep = Number(motor.step);
  const step = Number.isFinite(proposedStep) && proposedStep > 0 ? proposedStep : "any";
  return {
    id: Number(motor.id),
    key: String(motor.key ?? `motor_${motor.id}`),
    label: String(motor.label ?? `电机 ${motor.id}`),
    group: String(motor.group ?? ""),
    min,
    max,
    unit: String(motor.unit ?? ""),
    step,
  };
}

function isGripper(motor) {
  return motor.id === 14 || motor.id === 22 || motor.group === "gripper" || motor.group.endsWith("_gripper");
}

function createMotorControl(motor) {
  const row = document.createElement("div");
  row.className = "joint-row";
  row.dataset.motorId = String(motor.id);

  const rangeText = `${formatNumber(motor.min, motor.step)} … ${formatNumber(motor.max, motor.step)}`;
  row.innerHTML = `
    <div class="joint-name">
      <strong></strong>
      <small></small>
    </div>
    <div class="joint-current">当前<output>--</output></div>
    <input class="joint-slider motion-control" type="range" aria-label="目标" disabled>
    <input class="joint-input motion-control" type="number" inputmode="decimal" aria-label="目标数值" disabled>
    <button class="send-joint motion-control" type="button" aria-label="执行该关节目标" title="执行" disabled>›</button>`;

  row.querySelector(".joint-name strong").textContent = motor.label;
  row.querySelector(".joint-name small").textContent = `ID ${motor.id} · ${rangeText} ${motor.unit}`.trim();

  const slider = row.querySelector(".joint-slider");
  const input = row.querySelector(".joint-input");
  const button = row.querySelector(".send-joint");
  [slider, input].forEach((element) => {
    element.min = String(motor.min);
    element.max = String(motor.max);
    element.step = String(motor.step);
  });
  slider.value = String(motor.min);
  input.value = "";
  input.placeholder = "--";

  const elements = {
    row,
    output: row.querySelector("output"),
    slider,
    input,
    button,
    motor,
    seeded: false,
    dirty: false,
  };
  slider.addEventListener("input", () => {
    input.value = slider.value;
    elements.dirty = true;
  });
  input.addEventListener("input", () => {
    elements.dirty = true;
    const value = Number(input.value);
    if (Number.isFinite(value)) slider.value = String(clamp(value, motor.min, motor.max));
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") void sendMotorTarget(motor, row, input);
  });
  button.addEventListener("click", () => void sendMotorTarget(motor, row, input));

  state.motorElements.set(motor.id, elements);
  return row;
}

function renderMotionSpeedControl(config) {
  const values = {
    min: Number(config?.min),
    max: Number(config?.max),
    step: Number(config?.step),
    default: Number(config?.default),
  };
  if (!Number.isFinite(values.min) || !Number.isFinite(values.max) || values.max <= values.min ||
      !Number.isFinite(values.step) || values.step <= 0 || !Number.isFinite(values.default)) {
    state.motionSpeedConfig = null;
    dom.motionSpeedRange.disabled = true;
    dom.motionSpeedValue.textContent = "--";
    return;
  }
  values.default = clamp(values.default, values.min, values.max);
  state.motionSpeedConfig = values;
  dom.motionSpeedRange.min = String(values.min);
  dom.motionSpeedRange.max = String(values.max);
  dom.motionSpeedRange.step = String(values.step);
  dom.motionSpeedRange.value = String(values.default);
  const update = () => {
    const value = clamp(Number(dom.motionSpeedRange.value), values.min, values.max);
    dom.motionSpeedValue.textContent = formatSpeedScale(value);
  };
  dom.motionSpeedRange.addEventListener("input", update);
  update();
}

function getMotionSpeedScale() {
  const config = state.motionSpeedConfig;
  if (!config) return 1.0;
  const value = Number(dom.motionSpeedRange.value);
  return Number.isFinite(value) ? clamp(value, config.min, config.max) : config.default;
}

function formatSpeedScale(value) {
  return `${Number(value).toFixed(1)}×`;
}

function renderSpeedControls(chassis) {
  const wheelSpeed = normalizeWheelSpeedConfig(chassis?.wheel_speed);
  if (!wheelSpeed) {
    dom.speedControls.innerHTML = '<div class="panel-placeholder">后端未提供轮速配置</div>';
    return;
  }

  dom.speedControls.innerHTML = "";
  dom.speedControls.append(createWheelSpeedControl("wheelSpeed", wheelSpeed));
  const note = document.createElement("p");
  note.className = "drive-note";
  note.textContent = "SDK 未规定轮速数值限位，请从低速开始。松开、切页或失焦即停。";
  dom.speedControls.append(note);
}

function normalizeWheelSpeedConfig(config) {
  if (!config) return null;
  const defaultValue = Number(config.default);
  if (!Number.isFinite(defaultValue)) return null;
  return {
    label: String(config.label ?? "轮速"),
    default: Math.abs(defaultValue),
    unit: String(config.unit ?? "rad/s"),
  };
}

function createWheelSpeedControl(id, config) {
  const wrapper = document.createElement("div");
  wrapper.className = "speed-control speed-control--number";
  wrapper.innerHTML = `
    <label class="speed-label" for="${id}"><span></span><small></small></label>
    <div class="speed-number-wrap">
      <input class="speed-number motion-control" id="${id}" type="number" inputmode="decimal" step="any" disabled>
      <span></span>
    </div>`;
  const input = wrapper.querySelector("input");
  input.value = String(config.default);
  wrapper.querySelector(".speed-label span").textContent = config.label;
  wrapper.querySelector(".speed-label small").textContent = "左右轮幅值";
  wrapper.querySelector(".speed-number-wrap span").textContent = config.unit;
  return wrapper;
}

async function pollState() {
  try {
    const data = await getJson(API.state, 1800);
    state.lastStateAt = Date.now();
    applyRobotState(data);
  } catch (_) {
    if (Date.now() - state.lastStateAt > 2500) {
      applyConnection(false);
      if (state.takeover) applyTakeover(false);
    }
  }
}

function scheduleStatePoll() {
  window.clearTimeout(state.stateTimer);
  state.stateTimer = window.setTimeout(async () => {
    await pollState();
    scheduleStatePoll();
  }, 250);
}

function applyRobotState(data) {
  applyConnection(Boolean(data.connected), !data.server_owned && !data.sdk_loaded);
  state.sdkLoaded = Boolean(data.sdk_loaded ?? data.sdkLoaded);
  state.backendBusy = Boolean(data.busy);
  state.initState = Number.isFinite(Number(data.init_state)) ? Number(data.init_state) : null;
  dom.sdkText.textContent = state.sdkLoaded ? "就绪" : "待加载";
  const mode = data.control_mode_name ?? data.controlModeName ?? displayEnum(data.control_mode ?? data.controlMode);
  state.controlModeName = String(mode);
  dom.modeText.textContent = state.backendBusy || state.localBusy ? "执行中" : state.controlModeName;

  const backendTakeover = Boolean(data.takeover);
  if (!state.takeoverPending) {
    const owner = data.takeover_client_id ?? data.takeoverClientId ?? null;
    const ownerKnown = typeof owner === "string" && owner.length > 0;
    const ownedByThisPage = backendTakeover && Boolean(state.leaseId) &&
      (!ownerKnown || owner === state.clientId);
    if (!backendTakeover && state.takeover) {
      applyTakeover(false);
      clearLease();
      toast("控制租约已失效", "error");
    }
    if (backendTakeover && state.leaseId && ownerKnown && owner !== state.clientId) {
      applyTakeover(false);
      clearLease();
    } else if (ownedByThisPage && !state.takeover) {
      applyTakeover(true);
    }
    state.remoteTakeover = backendTakeover && !ownedByThisPage;
    if (state.remoteTakeover) dom.takeoverHint.textContent = "其他页面已接管";
    else if (!state.takeover) dom.takeoverHint.textContent = "已关闭";
  }

  const motors = data.motors ?? data.motor_states ?? {};
  state.motorElements.forEach((elements, id) => {
    const motorState = Array.isArray(motors)
      ? motors.find((item) => Number(item.id ?? item.motor_id) === id)
      : motors[id] ?? motors[String(id)];
    if (!motorState) return;
    const position = Number(motorState.position ?? motorState.Position);
    const error = Number(motorState.error_flag ?? motorState.error ?? 0);
    const readFailed = motorState.ok === false;
    if (Number.isFinite(position) && !readFailed && (!Number.isFinite(error) || error === 0)) {
      elements.output.textContent = `${formatNumber(position, elements.motor.step)} ${elements.motor.unit}`.trim();
      if ((!elements.seeded || !elements.dirty) && document.activeElement !== elements.input && document.activeElement !== elements.slider) {
        const value = clamp(position, elements.motor.min, elements.motor.max);
        const formatted = formatNumber(value, elements.motor.step);
        elements.input.value = formatted;
        elements.slider.value = formatted;
        elements.seeded = true;
      }
    }
    elements.row.classList.toggle("has-error", readFailed || (Number.isFinite(error) && error !== 0));
  });
  updateControlAvailability();
}

function applyConnection(connected, idle = false) {
  state.connected = connected;
  dom.connectionChip.classList.toggle("is-online", connected);
  dom.connectionChip.classList.toggle("is-offline", !connected && !idle);
  dom.connectionText.textContent = connected ? "机器人在线" : idle ? "待接管" : "机器人离线";
  updateControlAvailability();
}

function applyTakeover(enabled) {
  state.takeover = enabled;
  dom.takeoverToggle.checked = enabled;
  dom.takeoverHint.textContent = enabled ? "控制中" : "已关闭";
  dom.appShell.classList.toggle("is-taken-over", enabled);
  if (!enabled) stopDrive();
  updateControlAvailability();
}

function updateControlAvailability() {
  const enabled = canControl();
  document.querySelectorAll(".motion-control").forEach((control) => {
    control.disabled = !enabled;
  });
  // Never expose a default target for an actuator whose live feedback has not
  // arrived (or currently reports an SDK error), even when the global lease is
  // otherwise valid.
  state.motorElements.forEach((elements) => {
    const motorEnabled = enabled && elements.seeded && !elements.row.classList.contains("has-error");
    elements.slider.disabled = !motorEnabled;
    elements.input.disabled = !motorEnabled;
    elements.button.disabled = !motorEnabled;
  });
  // The backend intentionally loads/connects the SDK only after takeover, so
  // an offline-looking idle state must still allow the operator to acquire it.
  dom.takeoverToggle.disabled = state.takeoverPending || state.remoteTakeover || state.backendBusy || state.localBusy || pi05BlocksOtherControls();
  if (dom.voiceMotionToggle) {
    const canEnableVoiceMotion = canControl() && state.initState === 2 && state.voiceMotionReady;
    const canDisableVoiceMotion = state.voiceMotionEnabled && Boolean(state.leaseId);
    dom.voiceMotionToggle.disabled = pi05BlocksOtherControls() || state.voiceMotionPending || !(canEnableVoiceMotion || canDisableVoiceMotion);
  }
  updatePi05Controls();
}

function canControl() {
  return state.configLoaded && state.connected && state.sdkLoaded && !state.backendBusy && !state.localBusy &&
    !pi05BlocksOtherControls() && state.takeover && Boolean(state.leaseId) && !state.takeoverPending;
}

async function sendMotorTarget(motor, row, input) {
  if (!canControl()) return toast("请先接管控制", "error");
  if (!input.value.trim()) return toast(`请输入 ${motor.label} 目标`, "error");
  const target = Number(input.value);
  if (!Number.isFinite(target) || target < motor.min || target > motor.max) {
    return toast(`${motor.label} 目标超出 SDK 限位`, "error");
  }

  row.classList.add("is-sending");
  state.localBusy = true;
  updateControlAvailability();
  try {
    await postJson(
      API.joint,
      { motor_id: motor.id, target, speed_scale: getMotionSpeedScale() },
      35000,
      { lease: true },
    );
    const elements = state.motorElements.get(motor.id);
    if (elements) elements.dirty = false;
    toast(`${motor.label} 指令已发送`, "success", 1800);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.localBusy = false;
    row.classList.remove("is-sending");
    updateControlAvailability();
  }
}

async function runAction(name, confirmation) {
  if (!canControl()) return toast("请先接管控制", "error");
  stopDrive();
  if (!await confirmAction(confirmation)) return;
  if (!canControl()) return toast("确认期间控制状态已变化，动作未发送", "error", 5000);

  const button = { init: dom.initButton, deinit: dom.deinitButton, home: dom.homeButton }[name];
  const oldContent = button.innerHTML;
  state.localBusy = true;
  updateControlAvailability();
  button.textContent = "执行中…";
  try {
    const body = name === "home" ? { speed_scale: getMotionSpeedScale() } : {};
    const result = await postJson(API.action(name), body, 180000, { lease: true });
    toast(result.message ?? "动作已完成", "success", 4000);
  } catch (error) {
    toast(error.message, "error", 6000);
  } finally {
    state.localBusy = false;
    button.innerHTML = oldContent;
    updateControlAvailability();
  }
}

async function emergencyStop() {
  stopDrive();
  const emergencyLease = state.leaseId;
  state.pi05StopPending = true;
  updateControlAvailability();
  let inferenceStopped = false;
  let globalStopped = false;
  let inferenceError = null;
  let globalError = null;
  try {
    try {
      const result = await postJson(API.pi05Stop, {}, 9000);
      inferenceStopped = true;
      applyPi05Payload(result);
    } catch (error) {
      inferenceError = error;
    }

    if (emergencyLease) {
      try {
        await postJson(API.stop, {}, 12000, { lease: true, leaseId: emergencyLease });
        globalStopped = true;
      } catch (error) {
        globalError = error;
      }
    }
  } finally {
    state.pi05StopPending = false;
    await pollPi05();
    updateControlAvailability();
  }

  if (inferenceStopped && (!emergencyLease || globalStopped)) {
    const scope = globalStopped ? "推理和普通运动" : "推理";
    return toast(`软件急停已停止${scope}；这不是实体急停`, "success", 5200);
  }
  if (inferenceStopped) {
    return toast(`推理停止已发送，但普通运动停止未确认：${globalError?.message ?? "控制租约不可用"}。请使用实体急停`, "error", 8000);
  }
  if (globalStopped) {
    return toast(`普通运动停止已发送，但推理停止接口未确认：${inferenceError?.message ?? "未知错误"}。请使用实体急停`, "error", 8000);
  }
  const detail = [inferenceError?.message, globalError?.message].filter(Boolean).join("；");
  return toast(`软件急停未确认：${detail || "未知错误"}。请立即使用实体急停`, "error", 9000);
}

function startDrive(direction, button) {
  stopDrive();
  state.driveDirection = direction;
  button.classList.add("is-pressed");
  dom.driveState.classList.add("is-driving");
  dom.driveState.textContent = ({ forward: "前进", backward: "后退", left: "左转", right: "右转" })[direction];
  void driveTick();
  state.driveTimer = window.setInterval(driveTick, 120);
}

function stopDrive(options = {}) {
  const sendStop = options?.sendCommand !== false;
  const wasDriving = Boolean(state.driveDirection || state.driveTimer);
  window.clearInterval(state.driveTimer);
  state.driveTimer = null;
  state.driveDirection = null;
  document.querySelectorAll("[data-drive]").forEach((button) => button.classList.remove("is-pressed"));
  if (dom.driveState) {
    dom.driveState.classList.remove("is-driving");
    dom.driveState.textContent = "停止";
  }
  if (wasDriving && state.connected && sendStop) return sendChassis(0, 0, { quiet: true });
  return Promise.resolve();
}

async function driveTick() {
  if (!canControl() || !state.driveDirection) return stopDrive();
  const wheelSpeedControl = document.getElementById("wheelSpeed");
  if (!wheelSpeedControl) return stopDrive();
  const magnitude = Math.abs(Number(wheelSpeedControl.value));
  if (!Number.isFinite(magnitude)) {
    stopDrive();
    return toast("请输入有效轮速", "error");
  }
  const commands = {
    forward: [magnitude, magnitude],
    backward: [-magnitude, -magnitude],
    left: [-magnitude, magnitude],
    right: [magnitude, -magnitude],
  };
  const command = commands[state.driveDirection];
  if (command) await sendChassis(command[0], command[1], { quiet: true });
}

function sendChassis(leftSpeed, rightSpeed, { quiet = true } = {}) {
  if (!state.leaseId) {
    if (!quiet) toast("当前页面未接管控制", "error");
    return Promise.resolve();
  }
  // Coalesce repeated hold-to-drive refreshes. There is never more than one
  // chassis request in flight, so a release-time zero cannot be overtaken by
  // an older non-zero request on another HTTP connection.
  state.pendingChassis = { leftSpeed, rightSpeed, quiet };
  if (!state.chassisInFlight) state.chassisFlushPromise = flushChassisQueue();
  return state.chassisFlushPromise;
}

async function flushChassisQueue() {
  state.chassisInFlight = true;
  try {
    while (state.pendingChassis) {
      const command = state.pendingChassis;
      state.pendingChassis = null;
      try {
        await postJson(
          API.chassis,
          { left_speed: command.leftSpeed, right_speed: command.rightSpeed },
          1200,
          { lease: true },
        );
        if (!command.quiet) toast("底盘已停止", "success", 1600);
      } catch (error) {
        const wasDriving = Boolean(state.driveDirection);
        state.pendingChassis = null;
        stopDrive({ sendCommand: false });
        if (!command.quiet || wasDriving) toast(`底盘指令失败：${error.message}`, "error");
        break;
      }
    }
  } finally {
    state.chassisInFlight = false;
  }
}

function setStreamGroup(stream, enabled) {
  document.querySelectorAll(`.stream-frame[data-stream="${stream}"]`).forEach((frame) => {
    setStreamFrame(frame, enabled);
  });
  updateCameraStates();
  syncCameraSocket();
}

function setStreamFrame(frame, enabled) {
  const emptyTitle = frame.querySelector(".stream-empty span");
  const emptyHint = frame.querySelector(".stream-empty small");
  const stream = frame.dataset.stream;
  const streamKey = `${frame.dataset.camera}/${stream}`;

  frame.classList.remove("has-error", "is-waiting");
  if (enabled) {
    emptyTitle.textContent = "连接中";
    emptyHint.textContent = stream === "rgb" ? "RGB" : "DEPTH";
    frame.classList.add("is-on", "is-waiting");
  } else {
    frame.classList.remove("is-on", "has-error", "is-waiting");
    clearStreamImage(streamKey, frame);
    emptyTitle.textContent = stream === "rgb" ? "RGB" : "DEPTH";
    emptyHint.textContent = stream === "rgb" ? "请开启 RGB" : "请开启深度";
  }
}

function updateCameraStates() {
  const rgbEnabled = dom.rgbCamerasToggle.checked;
  const depthEnabled = dom.depthCamerasToggle.checked;
  document.querySelectorAll("[data-camera-heading]").forEach((heading) => {
    const status = heading.querySelector(".camera-state");
    heading.classList.toggle("is-streaming", rgbEnabled || depthEnabled);
    status.textContent = rgbEnabled && depthEnabled
      ? "RGB + 深度"
      : rgbEnabled
        ? "RGB"
        : depthEnabled
          ? "深度"
          : "已关闭";
  });
}

const CAMERA_STREAM_BY_ID = Object.freeze([
  "left_wrist/rgb",
  "left_wrist/depth",
  "head/rgb",
  "head/depth",
  "right_wrist/rgb",
  "right_wrist/depth",
]);

function activeCameraStreams() {
  const enabledTypes = [...document.querySelectorAll("[data-stream-group-toggle]:checked")]
    .map((toggle) => toggle.dataset.streamGroupToggle);
  return CAMERA_STREAM_BY_ID.filter((streamKey) => enabledTypes.includes(streamKey.split("/")[1]));
}

function syncCameraSocket() {
  const streams = activeCameraStreams();
  if (!streams.length) {
    closeCameraSocket();
    return;
  }

  const socket = state.cameraSocket;
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ streams }));
    return;
  }
  if (socket?.readyState === WebSocket.CONNECTING) return;
  openCameraSocket();
}

function openCameraSocket() {
  if (!activeCameraStreams().length) return;
  window.clearTimeout(state.cameraReconnectTimer);
  state.cameraReconnectTimer = null;
  markSelectedStreamsWaiting();

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  let socket;
  try {
    socket = new WebSocket(`${protocol}//${window.location.host}${API.cameraWs}`);
  } catch (_) {
    markSelectedStreamsError("连接失败", "正在重连");
    scheduleCameraReconnect();
    return;
  }
  socket.binaryType = "arraybuffer";
  state.cameraSocket = socket;

  socket.addEventListener("open", () => {
    if (socket !== state.cameraSocket) return;
    state.cameraReconnectDelay = 500;
    socket.send(JSON.stringify({ streams: activeCameraStreams() }));
  });

  socket.addEventListener("message", (event) => {
    if (socket !== state.cameraSocket) return;
    void handleCameraFrame(event.data);
  });

  socket.addEventListener("close", () => {
    if (socket !== state.cameraSocket) return;
    state.cameraSocket = null;
    if (!activeCameraStreams().length) return;
    markSelectedStreamsError("连接中断", "正在重连");
    scheduleCameraReconnect();
  });

  socket.addEventListener("error", () => {
    if (socket === state.cameraSocket) markSelectedStreamsError("连接失败", "正在重连");
  });
}

async function handleCameraFrame(data) {
  let buffer;
  if (data instanceof ArrayBuffer) {
    buffer = data;
  } else if (data instanceof Blob) {
    buffer = await data.arrayBuffer();
  } else {
    return;
  }
  if (buffer.byteLength < 2) return;

  const bytes = new Uint8Array(buffer);
  const streamKey = CAMERA_STREAM_BY_ID[bytes[0]];
  if (!streamKey || !activeCameraStreams().includes(streamKey)) return;
  const frame = findStreamFrame(streamKey);
  if (!frame) return;

  const img = frame.querySelector("img");
  const nextUrl = URL.createObjectURL(new Blob([bytes.subarray(1)], { type: "image/jpeg" }));
  const previousUrl = state.cameraObjectUrls.get(streamKey);
  state.cameraObjectUrls.set(streamKey, nextUrl);
  img.onerror = () => {
    if (img.src !== nextUrl) return;
    frame.classList.add("has-error");
    frame.classList.remove("is-waiting");
    frame.querySelector(".stream-empty span").textContent = "JPEG 解码失败";
    frame.querySelector(".stream-empty small").textContent = "等待下一帧";
  };
  img.src = nextUrl;
  if (previousUrl) URL.revokeObjectURL(previousUrl);
  frame.classList.remove("has-error", "is-waiting");
}

function findStreamFrame(streamKey) {
  const [camera, stream] = streamKey.split("/");
  return document.querySelector(`.stream-frame[data-camera="${camera}"][data-stream="${stream}"]`);
}

function clearStreamImage(streamKey, frame = findStreamFrame(streamKey)) {
  const objectUrl = state.cameraObjectUrls.get(streamKey);
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  state.cameraObjectUrls.delete(streamKey);
  const img = frame?.querySelector("img");
  if (img) {
    img.onerror = null;
    img.removeAttribute("src");
  }
}

function markSelectedStreamsWaiting() {
  activeCameraStreams().forEach((streamKey) => {
    const frame = findStreamFrame(streamKey);
    if (!frame) return;
    frame.classList.remove("has-error");
    frame.classList.add("is-waiting");
    frame.querySelector(".stream-empty span").textContent = "连接中";
    frame.querySelector(".stream-empty small").textContent = streamKey.endsWith("/rgb") ? "RGB" : "DEPTH";
  });
}

function markSelectedStreamsError(title, hint) {
  activeCameraStreams().forEach((streamKey) => {
    const frame = findStreamFrame(streamKey);
    if (!frame) return;
    clearStreamImage(streamKey, frame);
    frame.classList.remove("is-waiting");
    frame.classList.add("has-error");
    frame.querySelector(".stream-empty span").textContent = title;
    frame.querySelector(".stream-empty small").textContent = hint;
  });
}

function scheduleCameraReconnect() {
  window.clearTimeout(state.cameraReconnectTimer);
  const delay = state.cameraReconnectDelay;
  state.cameraReconnectDelay = Math.min(delay * 2, 8000);
  state.cameraReconnectTimer = window.setTimeout(openCameraSocket, delay);
}

function closeCameraSocket() {
  window.clearTimeout(state.cameraReconnectTimer);
  state.cameraReconnectTimer = null;
  state.cameraReconnectDelay = 500;
  const socket = state.cameraSocket;
  state.cameraSocket = null;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "streams disabled");
  CAMERA_STREAM_BY_ID.forEach((streamKey) => clearStreamImage(streamKey));
}

function confirmAction({ title, message, detail = "", accept = "确认执行", tone = "danger" }) {
  if (state.activeModalResolve) resolveModal(false);
  dom.confirmTitle.textContent = title;
  dom.confirmMessage.textContent = message;
  dom.confirmDetail.textContent = detail;
  dom.confirmDetail.hidden = !detail;
  dom.confirmAccept.textContent = accept;
  dom.confirmAccept.className = `button ${tone === "warning" ? "button--primary" : "button--danger"}`;
  dom.confirmIcon.textContent = tone === "warning" ? "↗" : "!";
  dom.confirmModal.hidden = false;
  document.body.style.overflow = "hidden";
  window.setTimeout(() => dom.confirmCancel.focus(), 0);
  return new Promise((resolve) => { state.activeModalResolve = resolve; });
}

function resolveModal(result) {
  if (!state.activeModalResolve) return;
  const resolve = state.activeModalResolve;
  state.activeModalResolve = null;
  dom.confirmModal.hidden = true;
  document.body.style.overflow = "";
  resolve(result);
}

function releaseOnExit() {
  window.clearInterval(state.driveTimer);
  window.clearInterval(state.heartbeatTimer);
  if (!state.leaseId) return;
  const headers = { "Content-Type": "application/json", "X-Control-Lease": state.leaseId };
  void fetch(API.chassis, {
    method: "POST", headers, body: JSON.stringify({ left_speed: 0, right_speed: 0 }), keepalive: true,
  });
  void fetch(API.takeover, {
    method: "POST", headers, body: JSON.stringify({ enabled: false }), keepalive: true,
  });
}

function startHeartbeat() {
  window.clearInterval(state.heartbeatTimer);
  state.heartbeatFailures = 0;
  void heartbeat();
  state.heartbeatTimer = window.setInterval(heartbeat, 800);
}

async function heartbeat() {
  if (!state.leaseId || !state.takeover) return;
  try {
    await postJson(API.heartbeat, {}, 1200, { lease: true });
    state.heartbeatFailures = 0;
  } catch (_) {
    state.heartbeatFailures += 1;
    if (state.heartbeatFailures >= 2) {
      stopDrive();
      applyTakeover(false);
      clearLease();
      toast("控制租约心跳中断，已锁定控制", "error", 5000);
    }
  }
}

function clearLease() {
  window.clearInterval(state.heartbeatTimer);
  state.heartbeatTimer = null;
  state.heartbeatFailures = 0;
  state.leaseId = null;
  state.voiceMotionEnabled = false;
  if (dom.voiceMotionToggle) dom.voiceMotionToggle.checked = false;
  updateControlAvailability();
}

function setPlaceholders(message) {
  [dom.leftArmControls, dom.rightArmControls, dom.bodyControls, dom.speedControls].forEach((target) => {
    target.innerHTML = `<div class="panel-placeholder">${escapeHtml(message)}</div>`;
  });
}

function displayEnum(value) {
  if (value == null) return "--";
  if (typeof value === "object") return String(value.name ?? value.value ?? "--");
  return String(value);
}

function formatNumber(value, step = "any") {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  if (step === "any") return numeric.toFixed(3);
  const stepNumber = Number(step);
  const precision = Number.isFinite(stepNumber) && stepNumber < 1
    ? Math.min(6, Math.max(2, Math.ceil(-Math.log10(stepNumber))))
    : 2;
  return numeric.toFixed(precision);
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

async function getJson(url, timeoutMs = 3000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: "no-store", signal: controller.signal });
    return await parseResponse(response);
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时");
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

async function postJson(url, body, timeoutMs = 15000, { lease = false, leaseId = null } = {}) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = { "Content-Type": "application/json" };
    if (lease) {
      const selectedLease = leaseId ?? state.leaseId;
      if (!selectedLease) throw new Error("缺少控制租约");
      headers["X-Control-Lease"] = selectedLease;
    }
    const response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    return await parseResponse(response);
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时");
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

async function parseResponse(response) {
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json") ? await response.json() : { message: await response.text() };
  if (!response.ok) throw new Error(payload.detail ?? payload.error ?? payload.message ?? `HTTP ${response.status}`);
  return payload;
}

function toast(message, tone = "", duration = 3200) {
  const item = document.createElement("div");
  item.className = `toast${tone ? ` is-${tone}` : ""}`;
  item.textContent = userFacingInferenceText(message);
  dom.toastRegion.append(item);
  window.setTimeout(() => item.remove(), duration);
}

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = String(value);
  return element.innerHTML;
}
