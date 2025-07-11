// /utils/auth.js This code file is suspect of server side????????
/**
 * Authentication and security utilities
 * Handles JWT tokens, session management, and security operations
 */

// import jwt from 'jsonwebtoken'; serverside code only
// import bcrypt from 'bcryptjs';
// import crypto from 'crypto';

import jwtDecode from 'jwt-decode';
import bcrypt from 'bcrypt-js';
import sha256 from 'js-sha256';

// Random number generation
const randomBytes = crypto.getRandomValues(new Uint8Array(16));

// Hashing
const sha256 = async (message) => {
  const encoder = new TextEncoder();
  const data = encoder.encode(message);
  const hash = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, '0')).join('');
};
// Authentication configuration
const AUTH_CONFIG = {
  method: 'JWT',
  tokenExpiry: '1h',
  refreshTokenExpiry: '7d',
  saltRounds: 12,
  encryption: {
    algorithm: 'aes-256-gcm',
    keyLength: 32,
    ivLength: 16
  }
};

// JWT secret keys (should be in environment variables)
const JWT_SECRET = process.env.JWT_SECRET || 'your-secret-key';
const JWT_REFRESH_SECRET = process.env.JWT_REFRESH_SECRET || 'your-refresh-secret-key';

/**
 * Generate JWT token for user authentication
 */
export const generateAccessToken = (payload) => {
  try {
    return jwt.sign(payload, JWT_SECRET, {
      expiresIn: AUTH_CONFIG.tokenExpiry,
      issuer: 'customer-service-web',
      audience: 'customer-service-users'
    });
  } catch (error) {
    console.error('Error generating access token:', error);
    throw new Error('Token generation failed');
  }
};

/**
 * Generate refresh token for token renewal
 */
export const generateRefreshToken = (payload) => {
  try {
    return jwt.sign(payload, JWT_REFRESH_SECRET, {
      expiresIn: AUTH_CONFIG.refreshTokenExpiry,
      issuer: 'customer-service-web',
      audience: 'customer-service-users'
    });
  } catch (error) {
    console.error('Error generating refresh token:', error);
    throw new Error('Refresh token generation failed');
  }
};

/**
 * Verify JWT access token
 */
export const verifyAccessToken = (token) => {
  try {
    return jwt.verify(token, JWT_SECRET, {
      issuer: 'customer-service-web',
      audience: 'customer-service-users'
    });
  } catch (error) {
    if (error.name === 'TokenExpiredError') {
      throw new Error('Token expired');
    } else if (error.name === 'JsonWebTokenError') {
      throw new Error('Invalid token');
    }
    throw new Error('Token verification failed');
  }
};

/**
 * Verify JWT refresh token
 */
export const verifyRefreshToken = (token) => {
  try {
    return jwt.verify(token, JWT_REFRESH_SECRET, {
      issuer: 'customer-service-web',
      audience: 'customer-service-users'
    });
  } catch (error) {
    throw new Error('Invalid refresh token');
  }
};

/**
 * Hash password using bcrypt
 */
export const hashPassword = async (password) => {
  try {
    const salt = await bcrypt.genSalt(AUTH_CONFIG.saltRounds);
    return await bcrypt.hash(password, salt);
  } catch (error) {
    console.error('Error hashing password:', error);
    throw new Error('Password hashing failed');
  }
};

/**
 * Verify password against hash
 */
export const verifyPassword = async (password, hash) => {
  try {
    return await bcrypt.compare(password, hash);
  } catch (error) {
    console.error('Error verifying password:', error);
    throw new Error('Password verification failed');
  }
};

/**
 * Generate secure random session ID
 */
export const generateSessionId = () => {
  return crypto.randomBytes(32).toString('hex');
};

/**
 * Generate secure random user ID
 */
export const generateUserId = () => {
  return crypto.randomUUID();
};

/**
 * Encrypt sensitive data
 */
export const encryptData = (data) => {
  try {
    const key = crypto.scryptSync(JWT_SECRET, 'salt', AUTH_CONFIG.encryption.keyLength);
    const iv = crypto.randomBytes(AUTH_CONFIG.encryption.ivLength);
    const cipher = crypto.createCipherGCM(AUTH_CONFIG.encryption.algorithm, key, iv);
    
    let encrypted = cipher.update(JSON.stringify(data), 'utf8', 'hex');
    encrypted += cipher.final('hex');
    
    const authTag = cipher.getAuthTag();
    
    return {
      encrypted,
      iv: iv.toString('hex'),
      authTag: authTag.toString('hex')
    };
  } catch (error) {
    console.error('Error encrypting data:', error);
    throw new Error('Data encryption failed');
  }
};

/**
 * Decrypt sensitive data
 */
export const decryptData = (encryptedData) => {
  try {
    const key = crypto.scryptSync(JWT_SECRET, 'salt', AUTH_CONFIG.encryption.keyLength);
    const decipher = crypto.createDecipherGCM(AUTH_CONFIG.encryption.algorithm, key, Buffer.from(encryptedData.iv, 'hex'));
    
    decipher.setAuthTag(Buffer.from(encryptedData.authTag, 'hex'));
    
    let decrypted = decipher.update(encryptedData.encrypted, 'hex', 'utf8');
    decrypted += decipher.final('utf8');
    
    return JSON.parse(decrypted);
  } catch (error) {
    console.error('Error decrypting data:', error);
    throw new Error('Data decryption failed');
  }
};

/**
 * Sanitize user input to prevent injection attacks
 */
export const sanitizeInput = (input) => {
  if (typeof input !== 'string') {
    return input;
  }
  
  return input
    .replace(/[<>\"']/g, '') // Remove potentially dangerous characters
    .trim()
    .substring(0, 1000); // Limit length
};

/**
 * Validate email format
 */
export const isValidEmail = (email) => {
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  return emailRegex.test(email);
};

/**
 * Validate password strength
 */
export const isValidPassword = (password) => {
  // At least 8 characters, 1 uppercase, 1 lowercase, 1 number
  const passwordRegex = /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[a-zA-Z\d@$!%*?&]{8,}$/;
  return passwordRegex.test(password);
};

/**
 * Generate CSRF token
 */
export const generateCSRFToken = () => {
  return crypto.randomBytes(32).toString('hex');
};

/**
 * Verify CSRF token
 */
export const verifyCSRFToken = (token, sessionToken) => {
  return token === sessionToken;
};

/**
 * Rate limiting utility
 */
export class RateLimiter {
  constructor(windowMs = 60000, maxRequests = 100) {
    this.windowMs = windowMs;
    this.maxRequests = maxRequests;
    this.requests = new Map();
  }

  isAllowed(identifier) {
    const now = Date.now();
    const windowStart = now - this.windowMs;
    
    // Clean up old entries
    for (const [key, value] of this.requests.entries()) {
      if (value.firstRequest < windowStart) {
        this.requests.delete(key);
      }
    }
    
    const userRequests = this.requests.get(identifier);
    
    if (!userRequests) {
      this.requests.set(identifier, {
        count: 1,
        firstRequest: now
      });
      return true;
    }
    
    if (userRequests.firstRequest < windowStart) {
      this.requests.set(identifier, {
        count: 1,
        firstRequest: now
      });
      return true;
    }
    
    if (userRequests.count >= this.maxRequests) {
      return false;
    }
    
    userRequests.count++;
    return true;
  }
}

/**
 * Session management utilities
 */
export const sessionUtils = {
  /**
   * Create new session
   */
  createSession: (userId, userAgent, ipAddress) => {
    const sessionId = generateSessionId();
    const createdAt = new Date();
    const expiresAt = new Date(createdAt.getTime() + 60 * 60 * 1000); // 1 hour
    
    return {
      sessionId,
      userId,
      userAgent,
      ipAddress,
      createdAt,
      expiresAt,
      isActive: true
    };
  },

  /**
   * Validate session
   */
  isValidSession: (session) => {
    if (!session) return false;
    if (!session.isActive) return false;
    if (new Date() > new Date(session.expiresAt)) return false;
    return true;
  },

  /**
   * Extend session expiry
   */
  extendSession: (session) => {
    const newExpiresAt = new Date(Date.now() + 60 * 60 * 1000); // 1 hour from now
    return {
      ...session,
      expiresAt: newExpiresAt
    };
  },

  /**
   * Invalidate session
   */
  invalidateSession: (session) => {
    return {
      ...session,
      isActive: false
    };
  }
};

/**
 * Permission checking utilities
 */
export const permissions = {
  USER: 'user',
  ADMIN: 'admin',
  MODERATOR: 'moderator',
  
  hasPermission: (userRole, requiredRole) => {
    const roleHierarchy = {
      user: 0,
      moderator: 1,
      admin: 2
    };
    
    return roleHierarchy[userRole] >= roleHierarchy[requiredRole];
  }
};

// Export configuration
export { AUTH_CONFIG };

// Create default rate limiter instance
export const defaultRateLimiter = new RateLimiter();