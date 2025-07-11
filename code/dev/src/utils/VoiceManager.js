// Voice recognition and synthesis utility
class VoiceManager {
  constructor() {
    this.recognition = null;
    this.synthesis = window.speechSynthesis;
    this.isListening = false;
    this.isSupported = this.checkSupport();
    this.currentLanguage = 'en-US';
    this.onResult = null;
    this.onError = null;
    this.onStart = null;
    this.onEnd = null;
    
    this.languageMap = {
      'en': 'en-US',
      'es': 'es-ES',
      'fr': 'fr-FR',
      'ar': 'ar-SA'
    };
    
    this.setupRecognition();
  }

  checkSupport() {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition) && 
           !!window.speechSynthesis;
  }

  setupRecognition() {
    if (!this.isSupported) return;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    this.recognition = new SpeechRecognition();
    
    this.recognition.continuous = false;
    this.recognition.interimResults = true;
    this.recognition.lang = this.currentLanguage;
    
    this.recognition.onstart = () => {
      this.isListening = true;
      if (this.onStart) this.onStart();
    };
    
    this.recognition.onresult = (event) => {
      let finalTranscript = '';
      let interimTranscript = '';
      
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript;
        
        if (event.results[i].isFinal) {
          finalTranscript += transcript;
        } else {
          interimTranscript += transcript;
        }
      }
      
      if (this.onResult) {
        this.onResult({
          final: finalTranscript,
          interim: interimTranscript,
          confidence: event.results[0] ? event.results[0][0].confidence : 0
        });
      }
    };
    
    this.recognition.onerror = (event) => {
      this.isListening = false;
      if (this.onError) {
        this.onError({
          error: event.error,
          message: this.getErrorMessage(event.error)
        });
      }
    };
    
    this.recognition.onend = () => {
      this.isListening = false;
      if (this.onEnd) this.onEnd();
    };
  }

  getErrorMessage(error) {
    const errorMessages = {
      'network': 'Network error occurred. Please check your connection.',
      'not-allowed': 'Microphone access denied. Please allow microphone access.',
      'no-speech': 'No speech detected. Please try again.',
      'aborted': 'Speech recognition was aborted.',
      'audio-capture': 'Audio capture failed. Please check your microphone.',
      'bad-grammar': 'Grammar error in speech recognition.',
      'language-not-supported': 'Language not supported.',
      'service-not-allowed': 'Speech service not allowed.'
    };
    
    return errorMessages[error] || 'An unknown error occurred during speech recognition.';
  }

  startListening(sessionId, language = 'en') {
    if (!this.isSupported) {
      throw new Error('Speech recognition is not supported in this browser.');
    }
    
    if (this.isListening) {
      this.stopListening();
    }
    
    this.setLanguage(language);
    
    try {
      this.recognition.start();
      return {
        success: true,
        sessionId: sessionId,
        language: this.currentLanguage
      };
    } catch (error) {
      return {
        success: false,
        error: error.message
      };
    }
  }

  stopListening() {
    if (this.recognition && this.isListening) {
      this.recognition.stop();
    }
  }

  setLanguage(language) {
    const speechLang = this.languageMap[language] || language;
    this.currentLanguage = speechLang;
    
    if (this.recognition) {
      this.recognition.lang = speechLang;
    }
  }

  speak(text, language = 'en', options = {}) {
    if (!this.synthesis) {
      throw new Error('Speech synthesis is not supported in this browser.');
    }
    
    // Cancel any ongoing speech
    this.synthesis.cancel();
    
    const utterance = new SpeechSynthesisUtterance(text);
    
    // Set language
    utterance.lang = this.languageMap[language] || language;
    
    // Set options
    utterance.rate = options.rate || 1;
    utterance.pitch = options.pitch || 1;
    utterance.volume = options.volume || 1;
    
    // Get available voices
    const voices = this.synthesis.getVoices();
    const preferredVoice = voices.find(voice => 
      voice.lang.startsWith(utterance.lang.substring(0, 2))
    );
    
    if (preferredVoice) {
      utterance.voice = preferredVoice;
    }
    
    // Set event handlers
    utterance.onstart = options.onStart || null;
    utterance.onend = options.onEnd || null;
    utterance.onerror = options.onError || null;
    
    this.synthesis.speak(utterance);
    
    return {
      success: true,
      utterance: utterance
    };
  }

  stopSpeaking() {
    if (this.synthesis) {
      this.synthesis.cancel();
    }
  }

  getAvailableVoices() {
    if (!this.synthesis) return [];
    
    return this.synthesis.getVoices().map(voice => ({
      name: voice.name,
      lang: voice.lang,
      gender: voice.name.toLowerCase().includes('female') ? 'female' : 'male',
      default: voice.default
    }));
  }

  isListeningActive() {
    return this.isListening;
  }

  isSpeechSupported() {
    return this.isSupported;
  }

  // Set event handlers
  onRecognitionStart(callback) {
    this.onStart = callback;
  }

  onRecognitionResult(callback) {
    this.onResult = callback;
  }

  onRecognitionError(callback) {
    this.onError = callback;
  }

  onRecognitionEnd(callback) {
    this.onEnd = callback;
  }

  // Utility method to transcribe audio (mock implementation)
  transcribeAudio(audioBlob, language = 'en') {
    // This would integrate with an actual speech-to-text API
    return new Promise((resolve, reject) => {
      // Mock implementation
      setTimeout(() => {
        resolve({
          text: 'Hello, I need help with my order',
          confidence: 0.85,
          language: language
        });
      }, 1000);
    });
  }

  // Check microphone permission
  async checkMicrophonePermission() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach(track => track.stop());
      return { granted: true };
    } catch (error) {
      return { 
        granted: false, 
        error: error.name === 'NotAllowedError' ? 'Permission denied' : error.message 
      };
    }
  }
}

// Create singleton instance
const voiceManager = new VoiceManager();

export default voiceManager;