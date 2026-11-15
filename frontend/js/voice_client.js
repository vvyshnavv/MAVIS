// =========================================================================
// MAVIS Autonomous Voice Intake Client v8.0
// Pure PCM WAV Encoding (Zero MediaRecorder Fragmentation / Zero Groq 400 Bad Request)
// Dual-Engine Speech Processing: 16-bit PCM WAV + Chrome Native Speech Recognition
// Real-Time 60FPS Level Meter, Hardware Device Selector & 2.5x Mic Boost
// =========================================================================

class MavisVoiceClient {
  constructor() {
    this.callId = null;
    this.stage = "name";
    this.callerPhone = "";
    this.patientName = "Patient";
    this.patientAge = 35;
    this.targetDepartment = null;
    this.symptomDescription = null;
    this.severity = 5;
    this.duration = "Recent";

    // Audio Graph State
    this.isPlayingAudio = false;
    this.isProcessingTurn = false;
    this.currentAudio = null;
    this.currentBufferSource = null;
    this.microphoneStream = null;
    this.audioContext = null;
    this.analyser = null;
    this.processor = null;
    this.micBoostNode = null;
    this.keepAliveGain = null;
    this.micBoostMultiplier = 2.5; // 2.5x digital boost for laptop microphones
    this.selectedDeviceId = "";
    this.animFrameId = null;
    this.speechWatchdogTimer = null;

    // Real-Time Sample Accumulation & VAD State
    this.preRollBuffer = [];
    this.speechSamples = [];
    this.isSpeaking = false;
    this.silenceStartTime = 0;
    this.speechStartTime = 0;
    this.isManualRecording = false;
    this.currentMicRms = 0.0;

    // Browser Native Speech Recognition Engine (Chrome/Edge internal fallback)
    this.speechRecognizer = null;
    this.speechRecognitionActive = false;
    this.speechRecognitionStarting = false;
    this.speechRecognitionRestartTimer = null;
    this.initNativeSpeechRecognition();

    // WebSocket Streaming Connection
    this.ws = null;
    this.wsConnected = false;

    // Cached Female Voice for browser synthesis fallback
    this.cachedFemaleVoice = null;
    if ("speechSynthesis" in window) {
      window.speechSynthesis.onvoiceschanged = () => {
        this.cachedFemaleVoice = this.getBestFemaleVoice();
      };
      this.cachedFemaleVoice = this.getBestFemaleVoice();
    }

    // Keyboard Spacebar Push-To-Talk shortcut
    window.addEventListener("keydown", (e) => {
      if (e.code === "Space" && e.target === document.body && this.callId && !this.isPlayingAudio && !this.isManualRecording) {
        e.preventDefault();
        this.startManualRecording();
      }
    });
    window.addEventListener("keyup", (e) => {
      if (e.code === "Space" && e.target === document.body && this.isManualRecording) {
        e.preventDefault();
        this.stopManualRecording();
      }
    });

    // Populate microphone devices on initial load
    setTimeout(() => this.enumerateAudioDevices(), 800);
  }

  // =========================================================================
  // DUAL-ENGINE: BROWSER NATIVE SPEECH RECOGNITION (ZERO-LATENCY TEXT)
  // =========================================================================
  initNativeSpeechRecognition() {
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRec) return;

    try {
      this.speechRecognizer = new SpeechRec();
      this.speechRecognizer.continuous = true;
      this.speechRecognizer.interimResults = false;
      this.speechRecognizer.lang = "en-US";

      this.speechRecognizer.onstart = () => {
        this.speechRecognitionStarting = false;
        this.speechRecognitionActive = true;
      };

      this.speechRecognizer.onresult = (event) => {
        if (this.isPlayingAudio || this.isProcessingTurn || !this.callId) return;

        for (let i = event.resultIndex; i < event.results.length; ++i) {
          if (event.results[i].isFinal) {
            const transcript = event.results[i][0].transcript.trim();
            if (transcript.length >= 2) {
              console.log("Chrome Native Speech Engine recognized:", transcript);
              // Directly submit turn with text transcript for instant, reliable response
              this.sendTurn(transcript, null);
              return;
            }
          }
        }
      };

      this.speechRecognizer.onerror = (e) => {
        if (e.error !== "no-speech" && e.error !== "aborted") {
          console.warn("SpeechRecognition notice:", e.error);
          if (this.callId && !this.isProcessingTurn) {
            this.setHtml("voice-status", `<span class="text-amber-600">Browser speech recognition error (${e.error}). Check browser/network permissions; audio detection remains available.</span>`);
          }
        }
      };

      this.speechRecognizer.onend = () => {
        this.speechRecognitionActive = false;
        this.speechRecognitionStarting = false;
        this.scheduleSpeechRecognitionRestart();
      };
    } catch (e) {
      console.warn("SpeechRecognition init exception:", e);
    }
  }

  getBestFemaleVoice() {
    if (!("speechSynthesis" in window)) return null;
    const voices = window.speechSynthesis.getVoices();
    if (!voices || voices.length === 0) return null;

    const preferred = [
      "zira", "jenny", "aria", "samantha", "victoria", "karen", "susan",
      "hazel", "serena", "moira", "fiona", "tessa", "allison", "ava",
      "zoe", "catherine", "heather", "stephanie", "linda", "female", "natural female"
    ];

    for (const name of preferred) {
      const match = voices.find(v => v.lang.startsWith("en") && v.name.toLowerCase().includes(name));
      if (match) return match;
    }

    const googleVoice = voices.find(v => v.lang.startsWith("en") && (
      v.name.includes("Google UK English Female") || 
      v.name.includes("Google US English") || 
      v.name.includes("Natural")
    ));
    if (googleVoice && !googleVoice.name.toLowerCase().includes("male") && !googleVoice.name.toLowerCase().includes("david") && !googleVoice.name.toLowerCase().includes("guy")) {
      return googleVoice;
    }

    const maleNames = ["david", "mark", "george", "richard", "guy", "james", "steven", "paul", "male", "microsoft david"];
    const notMale = voices.find(v => v.lang.startsWith("en") && !maleNames.some(m => v.name.toLowerCase().includes(m)));
    if (notMale) return notMale;

    return voices[0];
  }

  getTranscriptEl() {
    return document.getElementById("voice-transcript") || document.getElementById("transcript-container");
  }

  setHtml(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }

  toggleEl(id, show) {
    const el = document.getElementById(id);
    if (el) {
      if (show) el.classList.remove("hidden");
      else el.classList.add("hidden");
    }
  }

  // =========================================================================
  // HARDWARE MICROPHONE ENUMERATION & SWITCHING
  // =========================================================================
  async enumerateAudioDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;

    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const audioInputs = devices.filter(d => d.kind === "audioinput");
      const select = document.getElementById("mic-device-select");
      if (!select) return;

      select.innerHTML = "";
      if (audioInputs.length === 0) {
        select.innerHTML = `<option value="">Default Microphone</option>`;
        return;
      }

      audioInputs.forEach((d, idx) => {
        const opt = document.createElement("option");
        opt.value = d.deviceId;
        const label = d.label || `Microphone ${idx + 1}`;
        opt.text = label;
        if (!this.selectedDeviceId && (label.toLowerCase().includes("realtek") || label.toLowerCase().includes("array") || label.toLowerCase().includes("internal"))) {
          this.selectedDeviceId = d.deviceId;
          opt.selected = true;
        } else if (this.selectedDeviceId === d.deviceId) {
          opt.selected = true;
        }
        select.appendChild(opt);
      });
    } catch (e) {
      console.warn("enumerateAudioDevices notice:", e);
    }
  }

  setMicBoost(multiplierStr) {
    const val = parseFloat(multiplierStr) || 2.5;
    this.micBoostMultiplier = val;
    if (this.micBoostNode) {
      this.micBoostNode.gain.value = val;
      console.log("Updated microphone digital boost:", val);
    }
  }

  async switchMicrophone(deviceId) {
    this.selectedDeviceId = deviceId;
    console.log("Switching audio input device to:", deviceId);
    if (this.callId) {
      this.stopMicrophone();
      await this.initMicrophone();
    }
  }

  // =========================================================================
  // CALL LIFECYCLE MANAGEMENT
  // =========================================================================
  async startCall() {
    this.setHtml("voice-status", `<span class="inline-block w-2.5 h-2.5 rounded-full bg-amber-400 animate-ping mr-2"></span>Connecting to Autonomous Voice Model...`);

    const nameInp = document.getElementById("caller-name-input");
    const phoneInp = document.getElementById("caller-phone-input");
    if (nameInp) this.patientName = nameInp.value.trim() || "Patient";
    if (phoneInp) this.callerPhone = phoneInp.value.trim();

    try {
      // 1. Prime / initialize Web Audio context immediately inside user gesture click
      await this.ensureAudioContext();

      // 2. Prime DOM audio element
      const domPlayer = document.getElementById("mavis-audio-player");
      if (domPlayer) {
        domPlayer.play().catch(() => {});
      }

      // 3. Initiate voice session on backend
      const res = await fetch("/api/voice/session/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          caller_phone: this.callerPhone,
          client_type: "webrtc_browser"
        })
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Voice Gateway connection failed");
      }

      const data = await res.json();
      this.callId = data.call_id;
      this.stage = data.stage || "name";

      this.setHtml("voice-status", `<span class="inline-block w-2.5 h-2.5 rounded-full bg-emerald-400 mr-2"></span>Live Call Active: ${this.callId}`);

      // Update UI controls
      this.toggleEl("btn-start-call", false);
      this.toggleEl("btn-end-call", true);
      this.toggleEl("voice-waveform", true);

      // 4. Announce greeting immediately in natural female voice (Play FIRST)
      this.appendMessage("agent", data.system_speech);
      this.playAgentSpeech(data.system_speech, data.audio_base64, data.audio_mime);

      // 5. Initialize continuous hardware microphone with acoustic echo cancellation
      await this.initMicrophone();
      await this.enumerateAudioDevices();

      // 6. Connect bi-directional WebSocket stream
      this.connectWebSocket();

    } catch (err) {
      this.setHtml("voice-status", `<span class="text-rose-500 font-bold">Call Connection Error: ${err.message}</span>`);
      console.error("Voice startCall error:", err);
    }
  }

  async ensureAudioContext() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return null;

    if (!this.audioContext) {
      this.audioContext = new AudioContextClass();
    }
    if (this.audioContext.state === "suspended") {
      await this.audioContext.resume();
    }
    return this.audioContext;
  }

  async endCall() {
    this.stopAudio();
    this.stopMicrophone();
    this.stopSpeechRecognition();
    if (this.ws) {
      try { this.ws.close(); } catch (e) {}
      this.ws = null;
      this.wsConnected = false;
    }
    this.callId = null;
    this.stage = "name";
    this.targetDepartment = null;
    this.symptomDescription = null;
    this.isProcessingTurn = false;
    this.isSpeaking = false;
    this.speechSamples = [];

    // Reset Mic Meter Bar & Done button
    const bar = document.getElementById("mic-level-bar");
    const val = document.getElementById("mic-level-val");
    const dot = document.getElementById("mic-status-dot");
    const btnDone = document.getElementById("btn-done-speaking");
    if (bar) bar.style.width = "0%";
    if (val) val.innerText = "0%";
    if (dot) dot.className = "w-2 h-2 rounded-full bg-slate-300";
    if (btnDone) btnDone.classList.add("hidden");

    this.setHtml("voice-status", `<span class="text-slate-400">Call Disconnected · Session Ended</span>`);
    this.toggleEl("btn-start-call", true);
    this.toggleEl("btn-end-call", false);
    this.toggleEl("voice-waveform", false);
    this.toggleEl("agent-speaking-indicator", false);
  }

  // =========================================================================
  // CONTINUOUS HARDWARE MICROPHONE & PURE PCM SAMPLE ACCUMULATION
  // =========================================================================
  async initMicrophone() {
    try {
      await this.ensureAudioContext();
      if (!this.audioContext) return;

      // Echo cancellation is essential so MAVIS's voice does not feed back into mic
      const constraints = {
        audio: {
          deviceId: this.selectedDeviceId ? { exact: this.selectedDeviceId } : undefined,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      };

      this.microphoneStream = await navigator.mediaDevices.getUserMedia(constraints);

      const source = this.audioContext.createMediaStreamSource(this.microphoneStream);

      // Digital Gain Booster Node for under-amplified laptop microphones
      this.micBoostNode = this.audioContext.createGain();
      this.micBoostNode.gain.value = this.micBoostMultiplier || 2.5;
      source.connect(this.micBoostNode);

      // High-precision AnalyserNode for 60FPS volume & waveform tracking
      this.analyser = this.audioContext.createAnalyser();
      this.analyser.fftSize = 256;
      this.analyser.smoothingTimeConstant = 0.2;
      this.micBoostNode.connect(this.analyser);

      // Keep audio clock active in Chromium by routing tiny non-zero gain (0.0001 is inaudible)
      this.keepAliveGain = this.audioContext.createGain();
      this.keepAliveGain.gain.value = 0.0001;
      this.analyser.connect(this.keepAliveGain);
      this.keepAliveGain.connect(this.audioContext.destination);

      // ScriptProcessorNode for real-time Float32 PCM sample buffering
      const bufferSize = 4096;
      this.processor = this.audioContext.createScriptProcessor(bufferSize, 1, 1);
      this.micBoostNode.connect(this.processor);
      this.processor.connect(this.keepAliveGain);

      this.setupContinuousPCM();
      this.startWaveformVisualizer();

      console.log(`Continuous Hardware Microphone active at ${this.audioContext.sampleRate}Hz (Boost: ${this.micBoostMultiplier}x, AEC: ON).`);

    } catch (micErr) {
      console.warn("Microphone access notice:", micErr);
      const transcriptEl = this.getTranscriptEl();
      if (transcriptEl) {
        const notice = document.createElement("div");
        notice.className = "p-2.5 my-2 text-xs rounded-xl bg-amber-50 text-amber-800 border border-amber-200 text-center font-medium";
        notice.innerHTML = "🎙️ <strong>Microphone notice:</strong> Check your browser microphone permission, or use the text field below to continue.";
        transcriptEl.appendChild(notice);
      }
    }
  }

  setupContinuousPCM() {
    if (!this.processor) return;

    this.preRollBuffer = [];
    this.speechSamples = [];
    this.isSpeaking = false;
    this.silenceStartTime = 0;
    this.speechStartTime = 0;
    this.noiseFloor = 0.003;
    this.voiceLeadFrames = 0;
    this.bargeInFrameCount = 0;

    this.processor.onaudioprocess = (e) => {
      const channelData = e.inputBuffer.getChannelData(0);
      const frameSamples = new Float32Array(channelData);

      // Compute Root-Mean-Square (RMS)
      let sum = 0;
      for (let i = 0; i < frameSamples.length; i++) {
        sum += frameSamples[i] * frameSamples[i];
      }
      const rms = Math.sqrt(sum / frameSamples.length);
      this.currentMicRms = rms;

      // 1. Barge-in protection during agent speech
      if (this.isPlayingAudio) {
        // Protect first 2.0 seconds of agent speech so greeting is NEVER cut off
        if (Date.now() - (this.agentSpeechStartTime || 0) < 2000) {
          return;
        }
        // Require 4 consecutive loud speech frames (>0.08) to trigger barge-in
        if (rms > 0.08) {
          this.bargeInFrameCount = (this.bargeInFrameCount || 0) + 1;
          if (this.bargeInFrameCount >= 4) {
            this.bargeInFrameCount = 0;
            this.triggerBargeIn();
          }
        } else {
          this.bargeInFrameCount = 0;
        }
        return;
      }
      this.bargeInFrameCount = 0;

      // 2. Skip while awaiting server turn response
      if (!this.callId || this.isProcessingTurn) return;

      // 3. Manual Push-To-Talk Override
      if (this.isManualRecording) {
        for (let i = 0; i < frameSamples.length; i++) this.speechSamples.push(frameSamples[i]);
        return;
      }

      // 4. Adaptive Noise Floor Tracking
      if (!this.isSpeaking) {
        this.noiseFloor = (this.noiseFloor || 0.003) * 0.98 + rms * 0.02;
      }

      const speechThreshold = Math.max(0.008, (this.noiseFloor || 0.003) * 1.8);
      const silenceThreshold = Math.max(0.005, (this.noiseFloor || 0.003) * 1.3);
      const dot = document.getElementById("mic-status-dot");
      const btnDone = document.getElementById("btn-done-speaking");

      if (!this.isSpeaking) {
        if (rms > speechThreshold) {
          this.voiceLeadFrames = (this.voiceLeadFrames || 0) + 1;
          if (this.voiceLeadFrames >= 2) {
            this.isSpeaking = true;
            this.speechStartTime = Date.now();
            this.silenceStartTime = 0;
            this.voiceLeadFrames = 0;
            this.speechSamples = [];

            // Prepend pre-roll buffer so opening consonant is preserved
            for (const prev of this.preRollBuffer) {
              for (let i = 0; i < prev.length; i++) this.speechSamples.push(prev[i]);
            }

            if (dot) dot.className = "w-2 h-2 rounded-full bg-emerald-500 animate-ping";
            if (btnDone) btnDone.classList.remove("hidden");
            this.setHtml("voice-status", `<span class="inline-block w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse mr-2"></span>🎙️ Listening to caller (Voice detected)...`);
          }
        } else {
          this.voiceLeadFrames = 0;
          this.preRollBuffer.push(frameSamples);
          if (this.preRollBuffer.length > 5) this.preRollBuffer.shift();
        }

      } else {
        // Actively accumulating speech samples
        for (let i = 0; i < frameSamples.length; i++) this.speechSamples.push(frameSamples[i]);

        const elapsedSpeechMs = Date.now() - this.speechStartTime;

        // Hard timeout: auto-submit after 4.5s so recording NEVER gets stuck
        if (elapsedSpeechMs >= 4500) {
          console.log("Max speech duration reached (4.5s), auto-submitting audio turn...");
          this.finishAndSubmitAudio();
          return;
        }

        // Silence pause detection with hysteresis
        if (rms < silenceThreshold) {
          if (this.silenceStartTime === 0) {
            this.silenceStartTime = Date.now();
          } else if (Date.now() - this.silenceStartTime >= 650) {
            console.log(`Natural conversational pause (${Date.now() - this.silenceStartTime}ms). Submitting audio turn...`);
            this.finishAndSubmitAudio();
            return;
          }
        } else if (rms > speechThreshold * 1.1) {
          this.silenceStartTime = 0;
        }
      }
    };
  }

  // Completes caller speech turn and dispatches 16-bit PCM WAV to backend
  finishAndSubmitAudio() {
    if (!this.isSpeaking && this.speechSamples.length === 0) return;
    this.isSpeaking = false;
    this.silenceStartTime = 0;
    this.voiceLeadFrames = 0;

    const dot = document.getElementById("mic-status-dot");
    const btnDone = document.getElementById("btn-done-speaking");
    if (dot) dot.className = "w-2 h-2 rounded-full bg-slate-300";
    if (btnDone) btnDone.classList.add("hidden");

    const rate = this.audioContext ? this.audioContext.sampleRate : 48000;
    const minSamples = Math.round(rate * 0.25); // At least 250ms of audio

    if (this.speechSamples.length >= minSamples) {
      const wavBlob = this.encodeWAV(this.speechSamples, rate);
      this.speechSamples = [];
      this.sendAudioTurn(wavBlob);
    } else {
      this.speechSamples = [];
      this.resumeListening();
    }
  }

  // =========================================================================
  // PURE JAVASCRIPT 16-BIT PCM WAV ENCODER (ZERO CONTAINER CORRUPTION)
  // =========================================================================
  encodeWAV(samples, sampleRate) {
    const rate = sampleRate || (this.audioContext ? this.audioContext.sampleRate : 48000);
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);

    // RIFF chunk descriptor
    this.writeString(view, 0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    this.writeString(view, 8, "WAVE");

    // "fmt " sub-chunk
    this.writeString(view, 12, "fmt ");
    view.setUint32(16, 16, true);             // Subchunk1Size (16 for PCM)
    view.setUint16(20, 1, true);              // AudioFormat (1 = PCM)
    view.setUint16(22, 1, true);              // NumChannels (1 = Mono)
    view.setUint32(24, rate, true);           // SampleRate (e.g. 48000 or 44100)
    view.setUint32(28, rate * 2, true);       // ByteRate (rate * 1 channel * 2 bytes)
    view.setUint16(32, 2, true);              // BlockAlign (1 channel * 2 bytes)
    view.setUint16(34, 16, true);             // BitsPerSample (16-bit)

    // "data" sub-chunk
    this.writeString(view, 36, "data");
    view.setUint32(40, samples.length * 2, true);

    // Write 16-bit signed PCM samples
    let offset = 44;
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1.0, Math.min(1.0, samples[i]));
      view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
      offset += 2;
    }

    return new Blob([view], { type: "audio/wav" });
  }

  writeString(view, offset, str) {
    for (let i = 0; i < str.length; i++) {
      view.setUint8(offset + i, str.charCodeAt(i));
    }
  }

  // =========================================================================
  // REAL-TIME 60FPS WAVEFORM & TIME-DOMAIN VOLUME METER
  // =========================================================================
  startWaveformVisualizer() {
    const waveBars = document.querySelectorAll(".wave-bar");
    const bar = document.getElementById("mic-level-bar");
    const val = document.getElementById("mic-level-val");
    if (!this.analyser) return;

    const timeData = new Uint8Array(this.analyser.fftSize);
    const freqData = new Uint8Array(this.analyser.frequencyBinCount);

    const updateWave = () => {
      if (!this.analyser) return;

      // 1. Time-domain amplitude for live meter
      this.analyser.getByteTimeDomainData(timeData);
      let sumSquares = 0;
      for (let i = 0; i < timeData.length; i++) {
        const norm = (timeData[i] - 128) / 128.0;
        sumSquares += norm * norm;
      }
      const rms = Math.sqrt(sumSquares / timeData.length);

      const levelPct = Math.min(100, Math.round(rms * 450));
      if (bar) bar.style.width = `${levelPct}%`;
      if (val) val.innerText = `${levelPct}%`;

      // 2. Frequency data for animated bars
      this.analyser.getByteFrequencyData(freqData);
      let sumFreq = 0;
      for (let i = 0; i < freqData.length; i++) sumFreq += freqData[i];
      const avgFreq = sumFreq / freqData.length;

      if (waveBars && waveBars.length > 0) {
        waveBars.forEach((b, idx) => {
          const binValue = freqData[idx % freqData.length] || avgFreq;
          const scale = Math.max(0.18, Math.min(binValue / 75.0, 1.8));
          b.style.transform = `scaleY(${scale})`;
        });
      }

      this.animFrameId = requestAnimationFrame(updateWave);
    };

    updateWave();
  }

  stopMicrophone() {
    if (this.animFrameId) {
      cancelAnimationFrame(this.animFrameId);
      this.animFrameId = null;
    }
    if (this.processor) {
      try { this.processor.disconnect(); } catch (e) {}
      this.processor = null;
    }
    if (this.keepAliveGain) {
      try { this.keepAliveGain.disconnect(); } catch (e) {}
      this.keepAliveGain = null;
    }
    if (this.analyser) {
      try { this.analyser.disconnect(); } catch (e) {}
      this.analyser = null;
    }
    if (this.micBoostNode) {
      try { this.micBoostNode.disconnect(); } catch (e) {}
      this.micBoostNode = null;
    }
    if (this.microphoneStream) {
      this.microphoneStream.getTracks().forEach(t => t.stop());
      this.microphoneStream = null;
    }
  }

  // =========================================================================
  // TACTILE PUSH-TO-TALK CONTROLS (HOLD TO SPEAK / SPACEBAR)
  // =========================================================================
  startManualRecording() {
    this.stopAudio();
    this.stopSpeechRecognition();
    this.isManualRecording = true;
    this.speechSamples = [];
    const dot = document.getElementById("mic-status-dot");
    if (dot) dot.className = "w-2 h-2 rounded-full bg-rose-500 animate-ping";
    this.setHtml("voice-status", `<span class="inline-block w-2.5 h-2.5 rounded-full bg-rose-500 animate-pulse mr-2"></span>🎙️ Recording (Hold to Speak active)...`);
  }

  stopManualRecording() {
    if (!this.isManualRecording) return;
    this.isManualRecording = false;
    const dot = document.getElementById("mic-status-dot");
    const btnDone = document.getElementById("btn-done-speaking");
    if (dot) dot.className = "w-2 h-2 rounded-full bg-slate-300";
    if (btnDone) btnDone.classList.add("hidden");

    const rate = this.audioContext ? this.audioContext.sampleRate : 48000;
    if (this.speechSamples.length > (rate * 0.25)) {
      const wavBlob = this.encodeWAV(this.speechSamples, rate);
      this.speechSamples = [];
      this.sendAudioTurn(wavBlob);
    } else {
      this.speechSamples = [];
      this.resumeListening();
    }
  }

  sendAudioTurn(wavBlob) {
    if (!wavBlob || wavBlob.size < 400) {
      this.resumeListening();
      return;
    }
    this.setHtml("voice-status", `<span class="inline-block w-2.5 h-2.5 rounded-full bg-cyan-400 animate-pulse mr-2"></span>⚡ Groq Whisper Transcribing & Clinical Triage...`);

    const reader = new FileReader();
    reader.onloadend = () => {
      const base64Audio = reader.result.split(",")[1];
      this.sendTurn("", base64Audio);
    };
    reader.readAsDataURL(wavBlob);
  }

  // =========================================================================
  // BI-DIRECTIONAL WEBSOCKET STREAMING
  // =========================================================================
  connectWebSocket() {
    if (!this.callId) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/api/voice/ws/${this.callId}`;

    try {
      this.ws = new WebSocket(wsUrl);

      this.ws.onopen = () => {
        this.wsConnected = true;
        console.log("MAVIS WebSocket stream connected:", wsUrl);
      };

      this.ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "turn_response") {
            this.handleTurnResponse(msg);
          } else if (msg.type === "barge_in_acknowledged") {
            console.log("Barge-in acknowledged by server.");
          }
        } catch (e) {
          console.warn("WebSocket message parse error:", e);
        }
      };

      this.ws.onerror = (e) => {
        console.warn("WebSocket notice, falling back to HTTP turns:", e);
        this.wsConnected = false;
      };

      this.ws.onclose = () => {
        this.wsConnected = false;
      };

    } catch (e) {
      console.warn("WebSocket init error:", e);
      this.wsConnected = false;
    }
  }

  // =========================================================================
  // FAIL-SAFE NEURAL FEMALE SPEECH PLAYBACK (PLAYS ON EVERY SINGLE TURN)
  // Uses Web Audio decodeAudioData (immune to Autoplay restrictions)
  // =========================================================================
  async playAgentSpeech(text, audioB64, audioMime = "audio/mp3") {
    this.stopAudio();
    this.stopSpeechRecognition();
    this.isPlayingAudio = true;
    this.agentSpeechStartTime = Date.now();
    this.bargeInFrameCount = 0;
    this.toggleEl("agent-speaking-indicator", true);

    // 1st Priority: Web Audio decodeAudioData (Runs inside unlocked AudioContext, 100% immune to Autoplay policy)
    if (audioB64 && audioB64.length > 50 && this.audioContext) {
      try {
        if (this.audioContext.state === "suspended") {
          await this.audioContext.resume();
        }

        const binaryStr = atob(audioB64);
        const len = binaryStr.length;
        const bytes = new Uint8Array(len);
        for (let i = 0; i < len; i++) {
          bytes[i] = binaryStr.charCodeAt(i);
        }

        const audioBuffer = await this.audioContext.decodeAudioData(bytes.buffer.slice(0));
        const source = this.audioContext.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(this.audioContext.destination);

        this.currentBufferSource = source;
        source.onended = () => {
          if (this.currentBufferSource === source) {
            this.currentBufferSource = null;
            this.finishAgentSpeech();
          }
        };

        source.start(0);

        const durationMs = Math.round(audioBuffer.duration * 1000) + 600;
        if (this.speechWatchdogTimer) clearTimeout(this.speechWatchdogTimer);
        this.speechWatchdogTimer = setTimeout(() => {
          if (this.isPlayingAudio) {
            this.finishAgentSpeech();
          }
        }, durationMs);

        return;
      } catch (webaudioErr) {
        console.warn("WebAudio decodeAudioData playback failed, trying HTML5 Audio:", webaudioErr);
      }
    }

    // 2nd Priority: Persistent unlocked DOM audio element
    const domPlayer = document.getElementById("mavis-audio-player");
    if (domPlayer && audioB64 && audioB64.length > 50) {
      try {
        const mime = audioMime || "audio/mp3";
        domPlayer.src = `data:${mime};base64,${audioB64}`;
        domPlayer.onended = () => this.finishAgentSpeech();
        domPlayer.onerror = () => this.fallbackFemaleSpeech(text, () => this.finishAgentSpeech());

        const wordCount = (text || "").split(/\s+/).length;
        const maxDurationMs = Math.max(2500, (wordCount / 130) * 60 * 1000 + 2000);
        if (this.speechWatchdogTimer) clearTimeout(this.speechWatchdogTimer);
        this.speechWatchdogTimer = setTimeout(() => {
          if (this.isPlayingAudio) this.finishAgentSpeech();
        }, maxDurationMs);

        const playPromise = domPlayer.play();
        if (playPromise !== undefined) {
          playPromise.catch((err) => {
            console.warn("DOM audio player autoplay blocked:", err);
            this.fallbackFemaleSpeech(text, () => this.finishAgentSpeech());
          });
        }
        return;
      } catch (domErr) {
        console.warn("DOM audio player exception:", domErr);
      }
    }

    // 3rd Priority: Browser female speech synthesis
    this.fallbackFemaleSpeech(text, () => this.finishAgentSpeech());
  }

  finishAgentSpeech() {
    if (this.speechWatchdogTimer) {
      clearTimeout(this.speechWatchdogTimer);
      this.speechWatchdogTimer = null;
    }
    this.isPlayingAudio = false;
    this.toggleEl("agent-speaking-indicator", false);
    this.resumeListening();
  }

  fallbackFemaleSpeech(text, callback) {
    if (!("speechSynthesis" in window) || !text) {
      if (callback) callback();
      return;
    }

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.0;
    utterance.pitch = 1.15;
    utterance.lang = "en-US";

    const femaleVoice = this.cachedFemaleVoice || this.getBestFemaleVoice();
    if (femaleVoice) utterance.voice = femaleVoice;

    this.currentUtterance = utterance;

    utterance.onend = () => {
      this.currentUtterance = null;
      if (callback) callback();
    };
    utterance.onerror = () => {
      this.currentUtterance = null;
      if (callback) callback();
    };

    window.speechSynthesis.speak(utterance);
  }

  resumeListening() {
    if (!this.callId) return;
    this.isProcessingTurn = false;
    this.setHtml("voice-status", `<span class="inline-block w-2.5 h-2.5 rounded-full bg-emerald-400 mr-2"></span>🎙️ Ready for your response. Speak naturally into microphone.`);
    this.startSpeechRecognition();
  }

  startSpeechRecognition() {
    if (!this.speechRecognizer || !this.callId || this.isPlayingAudio || this.isProcessingTurn ||
        this.speechRecognitionActive || this.speechRecognitionStarting) return;

    if (this.speechRecognitionRestartTimer) {
      clearTimeout(this.speechRecognitionRestartTimer);
      this.speechRecognitionRestartTimer = null;
    }

    try {
      this.speechRecognitionStarting = true;
      this.speechRecognizer.start();
    } catch (err) {
      this.speechRecognitionStarting = false;
      console.warn("SpeechRecognition start notice:", err);
    }
  }

  scheduleSpeechRecognitionRestart() {
    if (!this.speechRecognizer || !this.callId || this.isPlayingAudio || this.isProcessingTurn) return;
    if (this.speechRecognitionRestartTimer) clearTimeout(this.speechRecognitionRestartTimer);
    this.speechRecognitionRestartTimer = setTimeout(() => {
      this.speechRecognitionRestartTimer = null;
      this.startSpeechRecognition();
    }, 250);
  }

  stopSpeechRecognition() {
    if (this.speechRecognitionRestartTimer) {
      clearTimeout(this.speechRecognitionRestartTimer);
      this.speechRecognitionRestartTimer = null;
    }
    if (!this.speechRecognizer) return;

    this.speechRecognitionStarting = false;
    if (!this.speechRecognitionActive) return;
    try {
      this.speechRecognizer.stop();
    } catch (err) {
      console.warn("SpeechRecognition stop notice:", err);
    }
  }

  stopAudio() {
    if (this.speechWatchdogTimer) {
      clearTimeout(this.speechWatchdogTimer);
      this.speechWatchdogTimer = null;
    }
    if (this.currentBufferSource) {
      try {
        this.currentBufferSource.stop();
        this.currentBufferSource.disconnect();
      } catch (e) {}
      this.currentBufferSource = null;
    }
    const domPlayer = document.getElementById("mavis-audio-player");
    if (domPlayer) {
      try {
        domPlayer.pause();
        domPlayer.currentTime = 0;
      } catch (e) {}
    }
    if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
    this.isPlayingAudio = false;
    this.toggleEl("agent-speaking-indicator", false);
  }

  // =========================================================================
  // TURN DISPATCH (WEBSOCKET STREAMING + HTTP FALLBACK)
  // =========================================================================
  async sendTurn(utterance = "", audioBase64 = null) {
    if (!utterance && !audioBase64) return;

    if (!this.callId) {
      await this.startCall();
    }

    this.stopAudio();
    this.isProcessingTurn = true;
    this.stopSpeechRecognition();

    if (utterance) {
      this.appendMessage("user", utterance);
    }

    this.setHtml("voice-status", `<span class="inline-block w-2.5 h-2.5 rounded-full bg-cyan-400 animate-pulse mr-2"></span>Processing through F4 Safety Gate & Clinical Engine...`);

    const payload = {
      type: "audio_turn",
      call_id: this.callId,
      caller_phone: this.callerPhone,
      caller_utterance: utterance || "",
      audio_base64: audioBase64 || null,
      current_stage: this.stage,
      patient_name: this.patientName,
      patient_age: parseInt(this.patientAge) || 35,
      target_department: this.targetDepartment,
      symptom_description: this.symptomDescription,
      severity: parseInt(this.severity) || 5,
      duration: this.duration || "Recent"
    };

    // 1. Try real-time WebSocket first if connected
    if (this.ws && this.wsConnected && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify(payload));
        return;
      } catch (wsErr) {
        console.warn("WebSocket send error, falling back to HTTP:", wsErr);
      }
    }

    // 2. High-reliability HTTP fallback
    try {
      const res = await fetch("/api/voice/process-turn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Turn processing failed");
      }

      const data = await res.json();
      this.handleTurnResponse(data);

    } catch (err) {
      this.setHtml("voice-status", `<span class="text-rose-500 font-bold">Processing Error: ${err.message}</span>`);
      console.error("Voice sendTurn error:", err);
      this.resumeListening();
    }
  }

  handleTurnResponse(data) {
    this.isProcessingTurn = false;

    // Display user's transcribed utterance if voice was sent
    if (data.caller_utterance && data.caller_utterance !== "Hello") {
      const transcriptEl = this.getTranscriptEl();
      const lastUserMsg = transcriptEl?.querySelector(".ml-auto:last-child");
      if (!lastUserMsg || !lastUserMsg.textContent.includes(data.caller_utterance)) {
        this.appendMessage("user", data.caller_utterance);
      }
    }

    // Update state tracking
    if (data.patient_name) {
      this.patientName = data.patient_name;
      const nameInp = document.getElementById("caller-name-input");
      if (nameInp) nameInp.value = this.patientName;
    }
    if (data.patient_age) this.patientAge = data.patient_age;
    if (data.target_department) this.targetDepartment = data.target_department;
    if (data.symptom_description) this.symptomDescription = data.symptom_description;
    if (data.severity) this.severity = data.severity;
    if (data.duration) this.duration = data.duration;

    if (data.red_flag_triggered) {
      this.stage = "emergency";
      this.setHtml("voice-status", `<span class="inline-block w-2.5 h-2.5 rounded-full bg-rose-500 animate-ping mr-2"></span>🚨 EMERGENCY RED FLAG DETECTED - Immediate Care Redirect`);
      this.appendEmergencyAlert(data);
      this.playAgentSpeech(data.system_speech, data.audio_base64, data.audio_mime);
    } else {
      this.stage = data.stage;
      this.setHtml("voice-status", `<span class="inline-block w-2.5 h-2.5 rounded-full bg-emerald-400 mr-2"></span>Stage: ${data.stage.toUpperCase()}`);
      this.appendMessage("agent", data.system_speech);

      if (data.booking_result && data.booking_result.token_id) {
        this.appendBookingCard(data.booking_result);
      }

      this.playAgentSpeech(data.system_speech, data.audio_base64, data.audio_mime);
    }
  }

  // Interruption / Barge-in trigger
  triggerBargeIn() {
    if (this.isPlayingAudio) {
      this.stopAudio();
      this.setHtml("voice-status", `<span class="inline-block w-2.5 h-2.5 rounded-full bg-amber-400 mr-2"></span>⚡ Interruption detected. Agent paused.`);

      if (this.ws && this.wsConnected) {
        try { this.ws.send(JSON.stringify({ type: "barge_in" })); } catch (e) {}
      }

      const transcriptEl = this.getTranscriptEl();
      if (transcriptEl) {
        const banner = document.createElement("div");
        banner.className = "p-2 my-2 text-xs rounded bg-amber-500/20 text-amber-700 border border-amber-500/40 text-center font-bold";
        banner.innerText = "⚡ Interrupted: Agent speech paused. Listening to caller...";
        transcriptEl.appendChild(banner);
        transcriptEl.scrollTop = transcriptEl.scrollHeight;
      }
      this.resumeListening();
    }
  }

  appendMessage(role, text) {
    if (!text || !text.trim()) return;
    const transcriptEl = this.getTranscriptEl();
    if (!transcriptEl) return;

    const placeholder = document.getElementById("voice-placeholder-msg");
    if (placeholder) placeholder.remove();

    const msgDiv = document.createElement("div");
    msgDiv.className = `p-3.5 rounded-2xl text-sm max-w-[85%] my-2 shadow-sm ${
      role === "user" 
        ? "ml-auto bg-[#0084ff] text-white rounded-tr-none font-medium" 
        : "mr-auto bg-white border border-blue-200 text-slate-800 rounded-tl-none"
    }`;
    msgDiv.innerHTML = `<div class="text-[10px] uppercase font-bold tracking-wider mb-1 ${role === "user" ? "text-blue-100 text-right" : "text-[#0084ff]"}">
      ${role === "user" ? "Caller (Voice Input)" : "MAVIS Voice Agent (Female Voice)"}
    </div><div>${text}</div>`;
    transcriptEl.appendChild(msgDiv);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  appendEmergencyAlert(data) {
    const transcriptEl = this.getTranscriptEl();
    if (!transcriptEl) return;

    const placeholder = document.getElementById("voice-placeholder-msg");
    if (placeholder) placeholder.remove();

    const alertDiv = document.createElement("div");
    alertDiv.className = "p-4 rounded-2xl border border-rose-300 bg-rose-50 text-rose-900 my-3 shadow-md";
    alertDiv.innerHTML = `
      <div class="flex items-center space-x-2 text-rose-700 font-extrabold mb-2">
        <svg class="w-5 h-5 animate-pulse text-rose-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
        <span>F4 ZERO-TOLERANCE RED-FLAG SAFETY GATE TRIGGERED</span>
      </div>
      <p class="text-xs text-rose-800 mb-2"><strong>Trigger Phrase Detected:</strong> "${data.trigger_phrase || 'Critical Symptom'}"</p>
      <p class="text-xs text-rose-800 mb-2"><strong>Clinical Rationale:</strong> ${data.clinical_rationale || 'Immediate emergency medical intervention indicated.'}</p>
      <div class="bg-white/90 p-3 rounded-xl border border-rose-200 text-xs mb-3 space-y-1">
        <div class="font-bold text-rose-900">Hospital Emergency Hotline:</div>
        <div>Emergency Lines: <span class="text-rose-700 font-mono font-bold">112 / 108</span> or Hospital Reception</div>
      </div>
      <div class="text-[11px] text-rose-600 font-mono">
        SHA-256 Audit Hash: ${data.record_hash ? data.record_hash.substring(0, 24) + '...' : 'Recorded in Tamper-Proof Chain'}
      </div>
    `;
    transcriptEl.appendChild(alertDiv);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  appendBookingCard(booking) {
    const transcriptEl = this.getTranscriptEl();
    if (!transcriptEl) return;

    const placeholder = document.getElementById("voice-placeholder-msg");
    if (placeholder) placeholder.remove();

    const card = document.createElement("div");
    card.className = "p-4 rounded-2xl border border-emerald-300 bg-emerald-50 text-emerald-900 my-3 shadow-md";
    card.innerHTML = `
      <div class="flex items-center justify-between mb-2">
        <span class="text-emerald-700 font-bold text-sm">✓ APPOINTMENT CONFIRMED</span>
        <span class="px-2.5 py-0.5 rounded-full text-xs font-bold bg-white text-emerald-700 border border-emerald-300 shadow-sm">Queue #${booking.queue_position}</span>
      </div>
      <div class="text-xs space-y-1 mb-3">
        <div><strong>Token ID:</strong> <span class="font-mono text-[#0084ff] text-sm font-bold">${booking.token_id}</span></div>
        <div><strong>Department:</strong> ${booking.department}</div>
        <div><strong>Doctor:</strong> ${booking.doctor_name || 'Department Specialist'} (${booking.room_number || 'OPD-101'})</div>
      </div>
      <button onclick="window.loadTokenInTracker('${booking.token_id}')" class="w-full py-2 px-3 rounded-full bg-emerald-600 hover:bg-emerald-700 text-white font-bold text-xs transition shadow-sm">
        Track Live Status in Patient Portal →
      </button>
    `;
    transcriptEl.appendChild(card);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;

    const cardToken = document.getElementById("card-token-id");
    const cardQueue = document.getElementById("card-queue-pos");
    const cardDoctor = document.getElementById("card-doctor");
    const cardSlot = document.getElementById("card-slot");
    if (cardToken) cardToken.innerText = booking.token_id;
    if (cardQueue) cardQueue.innerText = `#${booking.queue_position}`;
    if (cardDoctor) cardDoctor.innerText = `${booking.doctor_name || 'Specialist'} (${booking.room_number || 'OPD-101'})`;
    if (cardSlot && booking.slot_timestamp) cardSlot.innerText = new Date(booking.slot_timestamp).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
    this.toggleEl("booking-confirmed-card", true);
  }
}

window.voiceClient = new MavisVoiceClient();
