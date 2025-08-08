// src/context/AuthContext.js
import React, { createContext, useContext, useState, useEffect } from 'react';
import { authApi } from '../utils/api';

export const AuthContext = createContext();

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    console.error('useAuth must be used within an AuthProvider');
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [isAuthenticated, setIsAuthenticated] = useState(false);

  // Check for existing session on mount and validate token
  useEffect(() => {
    const validateToken = async () => {
      try {
        const token = localStorage.getItem('auth_token');
        
        if (token) {
          // Validate token with backend
          const userData = await authApi.getCurrentUser();
          setUser(userData);
          setIsAuthenticated(true);
        }
      } catch (error) {
        console.error('Token validation failed:', error);
        // Clear invalid token
        localStorage.removeItem('auth_token');
        localStorage.removeItem('user_id');
        setIsAuthenticated(false);
        setUser(null);
      } finally {
        setLoading(false);
      }
    };

    validateToken();
  }, []);

  const register = async (username, password, email, fullName) => {
    try {
      setLoading(true);
      
      const response = await fetch(`${import.meta.env.VITE_API_URL || 'http://localhost:8000'}/users/register`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          username,
          password,
          email,
          full_name: fullName
        }),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Registration failed');
      }

      const userData = await response.json();
      
      return { success: true, user: userData };
    } catch (error) {
      console.error('Registration error:', error);
      return { success: false, error: error.message };
    } finally {
      setLoading(false);
    }
  };

  const login = async (username, password) => {
    try {
      setLoading(true);
      
      // Call real API
      const response = await authApi.login({ username, password });
      
      // Get user data after successful login
      const userData = await authApi.getCurrentUser();
      
      setUser(userData);
      setIsAuthenticated(true);
      
      return { success: true, user: userData };
    } catch (error) {
      console.error('Login error:', error);
      return { success: false, error: error.message };
    } finally {
      setLoading(false);
    }
  };

  const logout = async () => {
    try {
      await authApi.logout();
      
      // Reset state
      setUser(null);
      setIsAuthenticated(false);
      
      return { success: true };
    } catch (error) {
      console.error('Logout error:', error);
      return { success: false, error: error.message };
    }
  };

  const updateProfile = async (profileData) => {
    try {
      // You would call a real API here
      const updatedUser = { ...user, ...profileData };
      setUser(updatedUser);
      
      return { success: true, user: updatedUser };
    } catch (error) {
      console.error('Profile update error:', error);
      return { success: false, error: error.message };
    }
  };

  const getAuthToken = () => {
    return localStorage.getItem('auth_token');
  };

  const isTokenValid = () => {
    const token = getAuthToken();
    return !!token;
  };

  const value = {
    user,
    loading,
    isAuthenticated,
    register,
    login,
    logout,
    updateProfile,
    getAuthToken,
    isTokenValid
  };

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
};