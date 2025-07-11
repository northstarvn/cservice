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
  MEETING: 'Meeting'
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

// Default Values
export const DEFAULTS = {
  LANGUAGE: LANGUAGES.EN,
  MAX_HISTORY: 100,
  RETENTION_PERIOD: '30 days',
  SESSION_TIMEOUT: 3600000, // 1 hour in milliseconds
  VECTOR_DIMENSION: 128,
  RATE_LIMIT: 100 // requests per minute
};

// Validation Rules
export const VALIDATION = {
  MIN_PASSWORD_LENGTH: 6,
  MAX_MESSAGE_LENGTH: 1000,
  MAX_PROJECT_NAME_LENGTH: 100,
  MAX_REQUIREMENTS_LENGTH: 2000,
  EMAIL_REGEX: /^[^\s@]+@[^\s@]+\.[^\s@]+$/,
  TRACKING_ID_REGEX: /^[A-Z0-9]{6,20}$/
};

// Time Formats
export const TIME_FORMATS = {
  DATETIME: 'YYYY-MM-DD HH:mm:ss',
  DATE: 'YYYY-MM-DD',
  TIME: 'HH:mm',
  TIMESTAMP: 'timestamp'
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

// Local Storage Keys
export const STORAGE_KEYS = {
  USER_PREFERENCES: 'user_preferences',
  CHAT_HISTORY: 'chat_history',
  CURRENT_LANGUAGE: 'current_language',
  USER_PROFILE: 'user_profile',
  SESSION_DATA: 'session_data'
};

// API Endpoints (placeholders)
export const API_ENDPOINTS = {
  CHAT: '/api/chat',
  BOOKING: '/api/booking',
  TRACKING: '/api/tracking',
  PROFILE: '/api/profile',
  PLANNING: '/api/planning',
  AUTH: '/api/auth',
  ANALYTICS: '/api/analytics'
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