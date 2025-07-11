import { VALIDATION, ERROR_MESSAGES } from './constants';

/**
 * Validation utility functions for form inputs and data
 */

// Base validation function
export const validate = (value, rules) => {
  const errors = [];

  if (rules.required && !value) {
    errors.push(ERROR_MESSAGES.REQUIRED_FIELD);
    return errors;
  }

  if (value) {
    if (rules.minLength && value.length < rules.minLength) {
      errors.push(`Minimum length is ${rules.minLength} characters`);
    }

    if (rules.maxLength && value.length > rules.maxLength) {
      errors.push(`Maximum length is ${rules.maxLength} characters`);
    }

    if (rules.pattern && !rules.pattern.test(value)) {
      errors.push(rules.patternMessage || 'Invalid format');
    }

    if (rules.custom && typeof rules.custom === 'function') {
      const customError = rules.custom(value);
      if (customError) {
        errors.push(customError);
      }
    }
  }

  return errors;
};

// Email validation
export const validateEmail = (email) => {
  return validate(email, {
    required: true,
    pattern: VALIDATION.EMAIL_REGEX,
    patternMessage: ERROR_MESSAGES.INVALID_EMAIL
  });
};

// Password validation
export const validatePassword = (password) => {
  return validate(password, {
    required: true,
    minLength: VALIDATION.MIN_PASSWORD_LENGTH,
    patternMessage: ERROR_MESSAGES.PASSWORD_TOO_SHORT
  });
};

// Username validation
export const validateUsername = (username) => {
  return validate(username, {
    required: true,
    minLength: 3,
    maxLength: 20,
    pattern: /^[a-zA-Z0-9_]+$/,
    patternMessage: 'Username can only contain letters, numbers, and underscores'
  });
};

// Full name validation
export const validateFullName = (fullName) => {
  return validate(fullName, {
    required: true,
    minLength: 2,
    maxLength: 50,
    pattern: /^[a-zA-Z\s]+$/,
    patternMessage: 'Full name can only contain letters and spaces'
  });
};

// Chat message validation
export const validateChatMessage = (message) => {
  return validate(message, {
    required: true,
    maxLength: VALIDATION.MAX_MESSAGE_LENGTH,
    custom: (value) => {
      if (value.trim().length === 0) {
        return 'Message cannot be empty';
      }
      return null;
    }
  });
};

// Tracking ID validation
export const validateTrackingId = (trackingId) => {
  return validate(trackingId, {
    required: true,
    pattern: VALIDATION.TRACKING_ID_REGEX,
    patternMessage: ERROR_MESSAGES.INVALID_TRACKING_ID
  });
};

// Project name validation
export const validateProjectName = (projectName) => {
  return validate(projectName, {
    required: true,
    minLength: 3,
    maxLength: VALIDATION.MAX_PROJECT_NAME_LENGTH,
    custom: (value) => {
      if (value.trim().length < 3) {
        return 'Project name must be at least 3 characters long';
      }
      return null;
    }
  });
};

// Project requirements validation
export const validateProjectRequirements = (requirements) => {
  return validate(requirements, {
    required: true,
    minLength: 10,
    maxLength: VALIDATION.MAX_REQUIREMENTS_LENGTH,
    custom: (value) => {
      if (value.trim().length < 10) {
        return 'Requirements must be at least 10 characters long';
      }
      return null;
    }
  });
};

// Date validation
export const validateDate = (date) => {
  const errors = [];
  
  if (!date) {
    errors.push(ERROR_MESSAGES.REQUIRED_FIELD);
    return errors;
  }

  const selectedDate = new Date(date);
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  if (selectedDate < today) {
    errors.push('Date cannot be in the past');
  }

  if (selectedDate.getTime() - today.getTime() > 365 * 24 * 60 * 60 * 1000) {
    errors.push('Date cannot be more than 1 year in the future');
  }

  return errors;
};

// Time validation
export const validateTime = (time) => {
  const errors = [];
  
  if (!time) {
    errors.push(ERROR_MESSAGES.REQUIRED_FIELD);
    return errors;
  }

  const timePattern = /^([01]?[0-9]|2[0-3]):[0-5][0-9]$/;
  if (!timePattern.test(time)) {
    errors.push('Invalid time format. Use HH:MM format');
  }

  return errors;
};

// Service type validation
export const validateServiceType = (serviceType) => {
  const validTypes = ['Consulting', 'Delivery', 'Meeting'];
  return validate(serviceType, {
    required: true,
    custom: (value) => {
      if (!validTypes.includes(value)) {
        return 'Invalid service type';
      }
      return null;
    }
  });
};

// Language validation
export const validateLanguage = (language) => {
  const validLanguages = ['en', 'es', 'fr', 'ar'];
  return validate(language, {
    required: true,
    custom: (value) => {
      if (!validLanguages.includes(value)) {
        return 'Invalid language selection';
      }
      return null;
    }
  });
};

// Form validation helper
export const validateForm = (formData, validationRules) => {
  const errors = {};
  let isValid = true;

  Object.keys(validationRules).forEach(field => {
    const fieldErrors = validate(formData[field], validationRules[field]);
    if (fieldErrors.length > 0) {
      errors[field] = fieldErrors;
      isValid = false;
    }
  });

  return { isValid, errors };
};

// Booking form validation
export const validateBookingForm = (formData) => {
  const errors = {};
  let isValid = true;

  // Validate service type
  const serviceTypeErrors = validateServiceType(formData.service_type);
  if (serviceTypeErrors.length > 0) {
    errors.service_type = serviceTypeErrors;
    isValid = false;
  }

  // Validate date
  const dateErrors = validateDate(formData.date);
  if (dateErrors.length > 0) {
    errors.date = dateErrors;
    isValid = false;
  }

  // Validate time
  const timeErrors = validateTime(formData.time);
  if (timeErrors.length > 0) {
    errors.time = timeErrors;
    isValid = false;
  }

  return { isValid, errors };
};

// Profile form validation
export const validateProfileForm = (formData) => {
  const errors = {};
  let isValid = true;

  // Validate full name
  const fullNameErrors = validateFullName(formData.full_name);
  if (fullNameErrors.length > 0) {
    errors.full_name = fullNameErrors;
    isValid = false;
  }

  // Validate email
  const emailErrors = validateEmail(formData.email);
  if (emailErrors.length > 0) {
    errors.email = emailErrors;
    isValid = false;
  }

  // Validate language
  const languageErrors = validateLanguage(formData.preferred_language);
  if (languageErrors.length > 0) {
    errors.preferred_language = languageErrors;
    isValid = false;
  }

  return { isValid, errors };
};

// Planning form validation
export const validatePlanningForm = (formData) => {
  const errors = {};
  let isValid = true;

  // Validate project name
  const projectNameErrors = validateProjectName(formData.project_name);
  if (projectNameErrors.length > 0) {
    errors.project_name = projectNameErrors;
    isValid = false;
  }

  // Validate requirements
  const requirementsErrors = validateProjectRequirements(formData.project_requirements);
  if (requirementsErrors.length > 0) {
    errors.project_requirements = requirementsErrors;
    isValid = false;
  }

  return { isValid, errors };
};

// Login form validation
export const validateLoginForm = (formData) => {
  const errors = {};
  let isValid = true;

  // Validate username
  const usernameErrors = validateUsername(formData.username);
  if (usernameErrors.length > 0) {
    errors.username = usernameErrors;
    isValid = false;
  }

  // Validate password
  const passwordErrors = validatePassword(formData.password);
  if (passwordErrors.length > 0) {
    errors.password = passwordErrors;
    isValid = false;
  }

  return { isValid, errors };
};

// Sanitize input to prevent XSS
export const sanitizeInput = (input) => {
  if (typeof input !== 'string') return input;
  
  return input
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')
    .replace(/\//g, '&#x2F;');
};

// Validate and sanitize form data
export const validateAndSanitizeForm = (formData, validationRules) => {
  const sanitizedData = {};
  
  // Sanitize all string fields
  Object.keys(formData).forEach(key => {
    sanitizedData[key] = sanitizeInput(formData[key]);
  });

  // Validate sanitized data
  const validation = validateForm(sanitizedData, validationRules);
  
  return {
    ...validation,
    sanitizedData
  };
};

// Check if value is empty (null, undefined, empty string, empty array)
export const isEmpty = (value) => {
  return value === null || value === undefined || value === '' || 
    (Array.isArray(value) && value.length === 0) ||
    (typeof value === 'object' && Object.keys(value).length === 0);
};

// Validate required fields
export const validateRequiredFields = (data, requiredFields) => {
  const missingFields = [];
  
  requiredFields.forEach(field => {
    if (isEmpty(data[field])) {
      missingFields.push(field);
    }
  });

  return {
    isValid: missingFields.length === 0,
    missingFields
  };
};