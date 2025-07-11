import React from 'react';
import { Loader2 } from 'lucide-react';

const LoadingSpinner = ({ size = 'md', text = 'Loading...', className = '', inline = false }) => {
  const getSizeClasses = () => {
    switch (size) {
      case 'sm':
        return 'w-4 h-4';
      case 'lg':
        return 'w-8 h-8';
      case 'xl':
        return 'w-12 h-12';
      case 'md':
      default:
        return 'w-6 h-6';
    }
  };

  const getTextSize = () => {
    switch (size) {
      case 'sm':
        return 'text-sm';
      case 'lg':
        return 'text-lg';
      case 'xl':
        return 'text-xl';
      case 'md':
      default:
        return 'text-base';
    }
  };

  if (inline) {
    return (
      <div className={`flex items-center space-x-2 ${className}`}>
        <Loader2 className={`${getSizeClasses()} animate-spin text-blue-600`} />
        {text && (
          <span className={`${getTextSize()} text-gray-600`}>
            {text}
          </span>
        )}
      </div>
    );
  }

  return (
    <div className={`flex flex-col items-center justify-center space-y-3 ${className}`}>
      <Loader2 className={`${getSizeClasses()} animate-spin text-blue-600`} />
      {text && (
        <span className={`${getTextSize()} text-gray-600 text-center`}>
          {text}
        </span>
      )}
    </div>
  );
};

// Full-screen loading overlay
export const LoadingOverlay = ({ isVisible, text = 'Loading...', className = '' }) => {
  if (!isVisible) return null;

  return (
    <div className={`fixed inset-0 bg-white bg-opacity-80 flex items-center justify-center z-50 ${className}`}>
      <div className="bg-white rounded-lg shadow-lg p-6 max-w-xs w-full mx-4">
        <LoadingSpinner size="lg" text={text} className="py-4" />
      </div>
    </div>
  );
};

// Loading skeleton for content
export const LoadingSkeleton = ({ className = '', lines = 3 }) => {
  return (
    <div className={`space-y-3 ${className}`}>
      {Array.from({ length: lines }).map((_, index) => (
        <div key={index} className="animate-pulse">
          <div className="h-4 bg-gray-200 rounded-md w-full"></div>
        </div>
      ))}
    </div>
  );
};

// Loading button state
export const LoadingButton = ({ 
  isLoading, 
  children, 
  loadingText = 'Loading...', 
  className = '',
  disabled = false,
  onClick,
  ...props 
}) => {
  return (
    <button
      onClick={onClick}
      disabled={disabled || isLoading}
      className={`flex items-center justify-center space-x-2 transition-all ${
        isLoading || disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'
      } ${className}`}
      {...props}
    >
      {isLoading && <Loader2 className="w-4 h-4 animate-spin" />}
      <span>{isLoading ? loadingText : children}</span>
    </button>
  );
};

// Loading state for lists
export const LoadingList = ({ items = 3, className = '' }) => {
  return (
    <div className={`space-y-4 ${className}`}>
      {Array.from({ length: items }).map((_, index) => (
        <div key={index} className="animate-pulse">
          <div className="flex items-center space-x-3">
            <div className="w-8 h-8 bg-gray-200 rounded-full"></div>
            <div className="flex-1 space-y-2">
              <div className="h-4 bg-gray-200 rounded-md w-3/4"></div>
              <div className="h-3 bg-gray-200 rounded-md w-1/2"></div>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
};

// Loading state for cards
export const LoadingCard = ({ className = '' }) => {
  return (
    <div className={`animate-pulse ${className}`}>
      <div className="bg-gray-200 rounded-lg p-4 space-y-3">
        <div className="h-6 bg-gray-300 rounded-md w-3/4"></div>
        <div className="space-y-2">
          <div className="h-4 bg-gray-300 rounded-md w-full"></div>
          <div className="h-4 bg-gray-300 rounded-md w-2/3"></div>
        </div>
        <div className="flex space-x-2">
          <div className="h-8 bg-gray-300 rounded-md w-20"></div>
          <div className="h-8 bg-gray-300 rounded-md w-16"></div>
        </div>
      </div>
    </div>
  );
};

export default LoadingSpinner;