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
        
        if (!token) {
          // No token found, set loading to false immediately
          setLoading(false);
          return;
        }

        console.log('Validating token...'); // Debug log
        
        // Add timeout to prevent hanging
        const timeoutPromise = new Promise((_, reject) => {
          setTimeout(() => reject(new Error('Request timeout')), 10000); // 10 second timeout
        });

        const userData = await Promise.race([
          authApi.getCurrentUser(),
          timeoutPromise
        ]);

        console.log('Token validation successful:', userData); // Debug log
        setUser(userData);
        setIsAuthenticated(true);
        
      } catch (error) {
        console.error('Token validation failed:', error);
        // Clear invalid or expired token
        localStorage.removeItem('auth_token');
        localStorage.removeItem('user_id');
        localStorage.removeItem('session_id');
        setIsAuthenticated(false);
        setUser(null);
      } finally {
        // Always set loading to false, regardless of success or failure
        console.log('Setting loading to false'); // Debug log
        setLoading(false);
      }
    };

    validateToken();
  }, []);

  const register = async (username, password, email, fullName) => {
    try {
      setLoading(true);
      
      const response = await authApi.register({
        username,
        password,
        email,
        fullName
      });
      
      return { success: true, user: response };
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
      
      if (!response.access_token) {
        throw new Error('No access token received');
      }

      console.log('Login successful, getting user data...'); // Debug log
      
      // Get user data after successful login with timeout
      const timeoutPromise = new Promise((_, reject) => {
        setTimeout(() => reject(new Error('User data fetch timeout')), 5000);
      });

      const userData = await Promise.race([
        authApi.getCurrentUser(),
        timeoutPromise
      ]);
      
      console.log('User data retrieved:', userData); // Debug log
      
      setUser(userData);
      setIsAuthenticated(true);
      
      return { success: true, user: userData };
    } catch (error) {
      console.error('Login error:', error);
      // Clear any partial auth state on login failure
      localStorage.removeItem('auth_token');
      localStorage.removeItem('user_id');
      localStorage.removeItem('session_id');
      setUser(null);
      setIsAuthenticated(false);
      
      return { success: false, error: error.message || 'Login failed' };
    } finally {
      setLoading(false);
    }
  };

  const logout = async () => {
    try {
      await authApi.logout();
    } catch (error) {
      console.error('Logout error:', error);
    } finally {
      // Always clear state regardless of API call success
      setUser(null);
      setIsAuthenticated(false);
      return { success: true };
    }
  };

  const updateProfile = async (profileData) => {
    try {
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