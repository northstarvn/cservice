// Application Constants
export const APP_CONFIG = {
  name: 'CustomerServiceWeb',
  version: '1.5.0',
  description: 'Customer service website with analytics, mobile support, CI/CD, and custom AI.'
};

// Route Constants
export const ROUTES = {
  HOME: '/',
  CHAT: '/chat',
  BOOKING: '/booking',
  TRACKING: '/tracking',
  PROFILE: '/profile',
  PLANNING: '/planning'
};

// Component IDs
export const COMPONENT_IDS = {
  MAIN_MENU: 'main_menu',
  CHAT_CONTAINER: 'chat_container',
  CHAT_INPUT: 'chat_input',
  VOICE_INPUT: 'voice_input',
  CHAT_HISTORY: 'chat_history',
  AI_SUGGESTIONS_PANEL: 'ai_suggestions_panel',
  BOOKING_FORM: 'booking_form',
  TRACKING_FORM: 'tracking_form',
  PROFILE_FORM: 'profile_form',
  PLANNING_FORM: 'planning_form'
};

// Screen IDs
export const SCREEN_IDS = {
  HOME: 'home_screen',
  CHAT: 'chat_screen',
  BOOKING: 'booking_screen',
  TRACKING: 'tracking_screen',
  PROFILE: 'profile_screen',
  PLANNING: 'planning_screen'
};

// Popup IDs
export const POPUP_IDS = {
  LOGIN: 'login_popup',
  BOOKING_CONFIRMATION: 'booking_confirmation_popup',
  TRACKING_RESULT: 'tracking_result_popup'
};

// Service Types
export const SERVICE_TYPES = {
  CONSULTING: 'Consulting',
  DELIVERY: 'Delivery',
  MEETING: 'Meeting',
  SUPPORT: 'Support',
  TRAINING: 'Training'
};

// Languages
export const LANGUAGES = {
  EN: 'en',
  ES: 'es',
  FR: 'fr',
  AR: 'ar'
};

export const RTL_LANGUAGES = ['ar'];

// Icon Files
export const ICONS = {
  HOME: 'home_icon.png',
  CHAT: 'chat_icon.png',
  CALENDAR: 'calendar_icon.png',
  TRACKING: 'tracking_icon.png',
  USER: 'user_icon.png',
  PLANNING: 'planning_icon.png',
  SEND: 'send_icon.png',
  LANGUAGE: 'language_icon.png',
  MIC: 'mic_icon.png',
  AI: 'ai_icon.png'
};

// Image Files
export const IMAGES = {
  WELCOME_BANNER: 'welcome_banner.jpg'
};

// Form Field Types
export const FIELD_TYPES = {
  TEXT: 'text',
  EMAIL: 'email',
  PASSWORD: 'password',
  TEXTAREA: 'textarea',
  SELECT: 'select',
  DATE: 'date',
  TIME: 'time',
  TEXT_AREA: 'text_area',
  VOICE_INPUT: 'voice_input'
};

// Button Actions
export const ACTIONS = {
  NAVIGATE_HOME: 'navigate_home',
  OPEN_CHAT: 'open_chat',
  NAVIGATE_BOOKING: 'navigate_booking',
  NAVIGATE_TRACKING: 'navigate_tracking',
  NAVIGATE_PROFILE: 'navigate_profile',
  NAVIGATE_PLANNING: 'navigate_planning',
  SEND_MESSAGE: 'send_message',
  SWITCH_LANGUAGE: 'switch_language',
  TOGGLE_VOICE_MODE: 'toggle_voice_mode',
  SUBMIT_BOOKING: 'submit_booking',
  CANCEL_BOOKING: 'cancel_booking',
  TRACK_DELIVERY: 'track_delivery',
  SAVE_PROFILE: 'save_profile',
  LOGOUT_USER: 'logout_user',
  SUBMIT_PLAN: 'submit_plan',
  AI_SUGGEST_REQUIREMENTS: 'ai_suggest_requirements',
  SUBMIT_LOGIN: 'submit_login',
  CLOSE_POPUP: 'close_popup',
  START_VOICE_RECOGNITION: 'start_voice_recognition'
};

// AI Response Modes
export const AI_MODES = {
  TEXT: 'text',
  SUGGESTIONS: 'suggestions',
  TRACKING_ASSIST: 'tracking_assist',
  SENTIMENT_ANALYSIS: 'sentiment_analysis'
};

// Mobile Platforms
export const MOBILE_PLATFORMS = ['mobile'];

// Event Types for Analytics
export const EVENT_TYPES = {
  CLICK: 'click',
  NAVIGATE: 'navigate',
  FORM_SUBMIT: 'form_submit',
  VOICE_INPUT: 'voice_input',
  LANGUAGE_SWITCH: 'language_switch',
  CHAT_MESSAGE: 'chat_message',
  BOOKING_CREATED: 'booking_created',
  TRACKING_SEARCHED: 'tracking_searched',
  PROFILE_UPDATED: 'profile_updated',
  PLAN_SUBMITTED: 'plan_submitted'
};

// Status Messages
export const STATUS_MESSAGES = {
  SUCCESS: 'success',
  ERROR: 'error',
  LOADING: 'loading',
  IDLE: 'idle'
};

// ✅ UPDATED: Client-side focused defaults
export const DEFAULTS = {
  LANGUAGE: LANGUAGES.EN,
  MAX_HISTORY: 100,
  MAX_CHAT_HISTORY_DISPLAY: 50,
  SESSION_TIMEOUT: 3600000, // 1 hour in milliseconds
  AUTO_SAVE_INTERVAL: 30000,
  DEBOUNCE_DELAY: 300,
  ANIMATION_DURATION: 200,
  RETRY_ATTEMPTS: 3,
  CACHE_DURATION: 300000,
  NOTIFICATION_DURATION: 5000
};

// Validation Rules
export const VALIDATION = {
  MIN_PASSWORD_LENGTH: 6,
  MAX_MESSAGE_LENGTH: 1000,
  MAX_PROJECT_NAME_LENGTH: 100,
  MAX_REQUIREMENTS_LENGTH: 2000,
  EMAIL_REGEX: /^[^\s@]+@[^\s@]+\.[^\s@]+$/,
  TRACKING_ID_REGEX: /^[A-Z0-9]{6,20}$/,
  PHONE_REGEX: /^[\+]?[1-9][\d]{0,15}$/,
  USERNAME_REGEX: /^[a-zA-Z0-9_]{3,20}$/
};

// ✅ UPDATED: Client-side time formats
export const TIME_FORMATS = {
  DISPLAY_DATE: 'MMM DD, YYYY',
  DISPLAY_TIME: 'h:mm A',
  DISPLAY_DATETIME: 'MMM DD, YYYY h:mm A',
  INPUT_DATE: 'YYYY-MM-DD',
  INPUT_TIME: 'HH:mm',
  INPUT_DATETIME: 'YYYY-MM-DDTHH:mm',
  ISO_STRING: 'toISOString',
  RELATIVE: 'relative'
};

// CSS Classes
export const CSS_CLASSES = {
  RTL: 'rtl',
  LTR: 'ltr',
  ACTIVE: 'active',
  DISABLED: 'disabled',
  LOADING: 'loading',
  ERROR: 'error',
  SUCCESS: 'success',
  HIDDEN: 'hidden',
  VISIBLE: 'visible'
};

// ✅ UPDATED: Enhanced storage configuration
export const STORAGE_KEYS = {
  // User Data
  USER_PREFERENCES: 'cservice_user_preferences',
  USER_PROFILE: 'cservice_user_profile',
  USER_SETTINGS: 'cservice_user_settings',
  
  // Session Management
  SESSION_DATA: 'cservice_session_data',
  AUTH_TOKEN: 'cservice_auth_token',
  REFRESH_TOKEN: 'cservice_refresh_token',
  
  // Application State
  CURRENT_LANGUAGE: 'cservice_current_language',
  THEME_PREFERENCE: 'cservice_theme_preference',
  CHAT_HISTORY: 'cservice_chat_history',
  FORM_DRAFTS: 'cservice_form_drafts',
  
  // Performance & Analytics
  CACHE_DATA: 'cservice_cache_data',
  ANALYTICS_CONSENT: 'cservice_analytics_consent',
  PERFORMANCE_METRICS: 'cservice_performance_metrics',
  
  // Feature States
  FEATURE_TUTORIALS: 'cservice_feature_tutorials',
  ACCESSIBILITY_SETTINGS: 'cservice_accessibility_settings'
};

// ✅ UPDATED: Environment-aware API endpoints
export const API_ENDPOINTS = {
  BASE_URL: typeof window !== 'undefined' && window.location.hostname === 'localhost' 
    ? 'http://localhost:3001' 
    : '',
  CHAT: '/api/chat',
  BOOKING: '/api/booking',
  TRACKING: '/api/tracking',
  PROFILE: '/api/profile',
  PLANNING: '/api/planning',
  AUTH: '/api/auth',
  ANALYTICS: '/api/analytics',
  HEALTH: '/api/health',
  VERSION: '/api/version'
};

// Error Messages
export const ERROR_MESSAGES = {
  NETWORK_ERROR: 'Network connection failed. Please try again.',
  VALIDATION_ERROR: 'Please check your input and try again.',
  SESSION_EXPIRED: 'Your session has expired. Please log in again.',
  ACCESS_DENIED: 'Access denied. Please check your permissions.',
  SERVER_ERROR: 'Server error occurred. Please try again later.',
  INVALID_TRACKING_ID: 'Invalid tracking ID format.',
  REQUIRED_FIELD: 'This field is required.',
  INVALID_EMAIL: 'Please enter a valid email address.',
  PASSWORD_TOO_SHORT: 'Password must be at least 6 characters long.'
};

// Success Messages
export const SUCCESS_MESSAGES = {
  BOOKING_CONFIRMED: 'Booking confirmed! Details sent to your email.',
  PROFILE_UPDATED: 'Profile updated successfully.',
  MESSAGE_SENT: 'Message sent successfully.',
  PLAN_SUBMITTED: 'Project plan submitted successfully.',
  LANGUAGE_SWITCHED: 'Language changed successfully.',
  LOGOUT_SUCCESS: 'Logged out successfully.'
};

// ✅ NEW: Client-side API Configuration
export const CLIENT_API_CONFIG = {
  TIMEOUT: 30000,
  RETRY_ATTEMPTS: 3,
  RETRY_DELAY: 1000,
  CONCURRENT_REQUESTS: 5,
  CACHE_STRATEGY: 'memory',
  OFFLINE_SUPPORT: true
};

// ✅ NEW: Client-side Performance Settings
export const PERFORMANCE_CONFIG = {
  LAZY_LOADING: true,
  VIRTUAL_SCROLLING_THRESHOLD: 100,
  IMAGE_OPTIMIZATION: true,
  BUNDLE_SPLITTING: true,
  PRELOAD_CRITICAL_RESOURCES: true
};

// ✅ NEW: Client-side Security Settings
export const CLIENT_SECURITY = {
  SANITIZE_HTML: true,
  VALIDATE_FORMS: true,
  SECURE_STORAGE: true,
  AUTO_LOGOUT_IDLE: 1800000,
  PREVENT_XSS: true,
  CONTENT_SECURITY_POLICY: true
};

// ✅ NEW: Browser Compatibility
export const BROWSER_SUPPORT = {
  MIN_CHROME_VERSION: 80,
  MIN_FIREFOX_VERSION: 75,
  MIN_SAFARI_VERSION: 13,
  MIN_EDGE_VERSION: 80,
  POLYFILLS_REQUIRED: ['fetch', 'IntersectionObserver']
};

// ✅ NEW: Client-side Feature Flags
export const FEATURE_FLAGS = {
  VOICE_RECOGNITION: true,
  OFFLINE_MODE: true,
  PUSH_NOTIFICATIONS: true,
  DARK_MODE: true,
  ANALYTICS_TRACKING: true,
  A11Y_ENHANCED: true,
  PWA_SUPPORT: true
};

// ✅ NEW: Enhanced Error Handling
export const NETWORK_ERROR_MESSAGES = {
  OFFLINE: 'You are currently offline. Please check your connection.',
  TIMEOUT: 'Request timed out. Please try again.',
  RATE_LIMITED: 'Too many requests. Please wait and try again.',
  CORS_ERROR: 'Access blocked by security policy.',
  DNS_ERROR: 'Cannot connect to server. Please check your connection.',
  SSL_ERROR: 'Security certificate error. Please check your connection.'
};

export const VALIDATION_ERROR_MESSAGES = {
  REQUIRED_FIELD: 'This field is required.',
  INVALID_EMAIL: 'Please enter a valid email address.',
  PASSWORD_TOO_SHORT: 'Password must be at least 6 characters long.',
  PASSWORD_WEAK: 'Password must contain uppercase, lowercase, number, and special character.',
  INVALID_PHONE: 'Please enter a valid phone number.',
  INVALID_DATE: 'Please enter a valid date.',
  INVALID_TIME: 'Please enter a valid time.',
  FILE_TOO_LARGE: 'File size must be less than 5MB.',
  INVALID_FILE_TYPE: 'File type not supported.'
};

// ✅ NEW: Storage Configuration
export const STORAGE_CONFIG = {
  PREFIX: 'cservice_',
  ENCRYPTION_ENABLED: true,
  COMPRESSION_ENABLED: true,
  MAX_STORAGE_SIZE: 5 * 1024 * 1024,
  CLEANUP_INTERVAL: 24 * 60 * 60 * 1000,
  STORAGE_TYPES: {
    PERSISTENT: 'localStorage',
    SESSION: 'sessionStorage',
    MEMORY: 'memoryStorage'
  }
};