// Replace entire utils/auth.js with client-safe version:

/**
 * Client-side authentication utilities
 * Handles token storage, validation, and client-side security
 */

import { jwtDecode } from 'jwt-decode';

// Client-side auth configuration
const CLIENT_AUTH_CONFIG = {
  tokenKey: 'auth_token',
  refreshTokenKey: 'refresh_token',
  userKey: 'user_data',
  sessionTimeout: 3600000, // 1 hour
};

/**
 * Client-side token decoding and validation
 */
export const decodeToken = (token) => {
  try {
    const decoded = jwtDecode(token);
    // Check if token is expired
    if (decoded.exp * 1000 < Date.now()) {
      throw new Error('Token expired');
    }
    return decoded;
  } catch (error) {
    console.error('Token decode error:', error);
    throw new Error('Invalid token');
  }
};

/**
 * Check if token is valid and not expired
 */
export const isTokenValid = (token) => {
  try {
    decodeToken(token);
    return true;
  } catch {
    return false;
  }
};

/**
 * Get stored auth token
 */
export const getAuthToken = () => {
  return localStorage.getItem(CLIENT_AUTH_CONFIG.tokenKey);
};

/**
 * Store auth token
 */
export const setAuthToken = (token) => {
  localStorage.setItem(CLIENT_AUTH_CONFIG.tokenKey, token);
};

/**
 * Remove auth token
 */
export const removeAuthToken = () => {
  localStorage.removeItem(CLIENT_AUTH_CONFIG.tokenKey);
  localStorage.removeItem(CLIENT_AUTH_CONFIG.refreshTokenKey);
  localStorage.removeItem(CLIENT_AUTH_CONFIG.userKey);
};

/**
 * Client-side session management
 */
export const createSession = (userData) => {
  const sessionData = {
    ...userData,
    timestamp: Date.now(),
    expiresAt: Date.now() + CLIENT_AUTH_CONFIG.sessionTimeout
  };
  localStorage.setItem(CLIENT_AUTH_CONFIG.userKey, JSON.stringify(sessionData));
};

/**
 * Validate client session
 */
export const validateSession = () => {
  const sessionData = localStorage.getItem(CLIENT_AUTH_CONFIG.userKey);
  if (!sessionData) return false;
  
  try {
    const session = JSON.parse(sessionData);
    return session.expiresAt > Date.now();
  } catch {
    return false;
  }
};

/**
 * Generate client-side random ID
 */
export const generateClientId = () => {
  return crypto.randomUUID();
};

/**
 * Client-side password strength validation
 */
export const validatePasswordStrength = (password) => {
  const minLength = 8;
  const hasUpper = /[A-Z]/.test(password);
  const hasLower = /[a-z]/.test(password);
  const hasNumber = /\d/.test(password);
  const hasSpecial = /[!@#$%^&*(),.?":{}|<>]/.test(password);
  
  return {
    isValid: password.length >= minLength && hasUpper && hasLower && hasNumber && hasSpecial,
    requirements: {
      minLength: password.length >= minLength,
      hasUpper,
      hasLower,
      hasNumber,
      hasSpecial
    }
  };
};

