/**
 * Client-side data management utilities
 * Handles local storage, session management, and API data caching
 */

class ClientDataManager {
  constructor() {
    this.cache = new Map();
    this.prefix = 'cservice_';
  }

  // Cache management for API responses
  setCache(key, data, ttl = 300000) { // 5 minutes default
    this.cache.set(this.prefix + key, {
      data,
      timestamp: Date.now(),
      ttl
    });
  }

  getCache(key) {
    const cached = this.cache.get(this.prefix + key);
    if (!cached) return null;
    
    if (Date.now() - cached.timestamp > cached.ttl) {
      this.cache.delete(this.prefix + key);
      return null;
    }
    
    return cached.data;
  }

  // Local storage helpers
  setLocalData(key, data) {
    try {
      localStorage.setItem(this.prefix + key, JSON.stringify(data));
      return true;
    } catch (error) {
      console.error('Local storage error:', error);
      return false;
    }
  }

  getLocalData(key) {
    try {
      const data = localStorage.getItem(this.prefix + key);
      return data ? JSON.parse(data) : null;
    } catch (error) {
      console.error('Local storage parse error:', error);
      return null;
    }
  }
}

export const clientDataManager = new ClientDataManager();