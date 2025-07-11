/**
 * Storage utility for managing application state
 * Handles session storage, user preferences, and temporary data
 */

class StorageManager {
  constructor() {
    this.prefix = 'csw_';
    this.sessionData = new Map();
    this.userPreferences = new Map();
    this.tempData = new Map();
  }

  // Session management
  setSession(key, value) {
    const prefixedKey = this.prefix + key;
    this.sessionData.set(prefixedKey, {
      value,
      timestamp: Date.now()
    });
    return true;
  }

  getSession(key) {
    const prefixedKey = this.prefix + key;
    const data = this.sessionData.get(prefixedKey);
    if (!data) return null;
    
    // Check if session is still valid (1 hour)
    if (Date.now() - data.timestamp > 3600000) {
      this.sessionData.delete(prefixedKey);
      return null;
    }
    
    return data.value;
  }

  clearSession(key) {
    const prefixedKey = this.prefix + key;
    this.sessionData.delete(prefixedKey);
  }

  clearAllSessions() {
    this.sessionData.clear();
  }

  // User preferences
  setUserPreference(userId, key, value) {
    const userKey = `${userId}_${key}`;
    this.userPreferences.set(userKey, value);
    return true;
  }

  getUserPreference(userId, key) {
    const userKey = `${userId}_${key}`;
    return this.userPreferences.get(userKey) || null;
  }

  getAllUserPreferences(userId) {
    const userPrefs = {};
    this.userPreferences.forEach((value, key) => {
      if (key.startsWith(userId + '_')) {
        const prefKey = key.substring(userId.length + 1);
        userPrefs[prefKey] = value;
      }
    });
    return userPrefs;
  }

  // Temporary data with TTL
  setTempData(key, value, ttl = 300000) { // 5 minutes default
    const prefixedKey = this.prefix + 'temp_' + key;
    this.tempData.set(prefixedKey, {
      value,
      expiry: Date.now() + ttl
    });
    return true;
  }

  getTempData(key) {
    const prefixedKey = this.prefix + 'temp_' + key;
    const data = this.tempData.get(prefixedKey);
    
    if (!data) return null;
    
    if (Date.now() > data.expiry) {
      this.tempData.delete(prefixedKey);
      return null;
    }
    
    return data.value;
  }

  clearTempData(key) {
    const prefixedKey = this.prefix + 'temp_' + key;
    this.tempData.delete(prefixedKey);
  }

  // Chat history storage
  setChatHistory(sessionId, messages) {
    return this.setSession(`chat_${sessionId}`, messages);
  }

  getChatHistory(sessionId) {
    return this.getSession(`chat_${sessionId}`) || [];
  }

  addChatMessage(sessionId, message) {
    const history = this.getChatHistory(sessionId);
    history.push({
      ...message,
      timestamp: Date.now(),
      id: this.generateId()
    });
    return this.setChatHistory(sessionId, history);
  }

  // Booking data
  setBookingData(bookingId, data) {
    return this.setTempData(`booking_${bookingId}`, data, 86400000); // 24 hours
  }

  getBookingData(bookingId) {
    return this.getTempData(`booking_${bookingId}`);
  }

  // Form data persistence
  saveFormData(formId, data) {
    return this.setTempData(`form_${formId}`, data, 1800000); // 30 minutes
  }

  getFormData(formId) {
    return this.getTempData(`form_${formId}`);
  }

  clearFormData(formId) {
    this.clearTempData(`form_${formId}`);
  }

  // Language settings
  setLanguage(userId, language) {
    return this.setUserPreference(userId, 'language', language);
  }

  getLanguage(userId) {
    return this.getUserPreference(userId, 'language') || 'en';
  }

  // Theme settings
  setTheme(userId, theme) {
    return this.setUserPreference(userId, 'theme', theme);
  }

  getTheme(userId) {
    return this.getUserPreference(userId, 'theme') || 'light';
  }

  // Utility methods
  generateId() {
    return Date.now().toString(36) + Math.random().toString(36).substr(2);
  }

  // Cleanup expired data
  cleanup() {
    const now = Date.now();
    
    // Cleanup sessions
    this.sessionData.forEach((data, key) => {
      if (now - data.timestamp > 3600000) {
        this.sessionData.delete(key);
      }
    });
    
    // Cleanup temp data
    this.tempData.forEach((data, key) => {
      if (now > data.expiry) {
        this.tempData.delete(key);
      }
    });
  }

  // Get storage stats
  getStorageStats() {
    return {
      sessions: this.sessionData.size,
      preferences: this.userPreferences.size,
      tempData: this.tempData.size,
      totalSize: this.sessionData.size + this.userPreferences.size + this.tempData.size
    };
  }
}

// Singleton instance
const storageManager = new StorageManager();

// Auto cleanup every 5 minutes
setInterval(() => {
  storageManager.cleanup();
}, 300000);

export default storageManager;