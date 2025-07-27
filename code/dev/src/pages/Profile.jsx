import React, { useState, useContext, useEffect } from 'react';
import { AppContext } from '../context/AppContext';
import { AuthContext } from '../context/AuthContext';
import LoadingSpinner from '../components/LoadingSpinner';

const Profile = () => {
  const { addNotification, isLoading, setIsLoading, currentLanguage, switchLanguage } = useContext(AppContext);
  const { user, updateUserProfile, logout } = useContext(AuthContext);
  
  const [formData, setFormData] = useState({
    full_name: '',
    email: '',
    preferred_language: 'en'
  });
  
  const [isEditing, setIsEditing] = useState(false);
  const [originalData, setOriginalData] = useState({});

  const languages = [
    { code: 'en', name: 'English', flag: '🇺🇸' },
    { code: 'es', name: 'Español', flag: '🇪🇸' },
    { code: 'fr', name: 'Français', flag: '🇫🇷' },
    { code: 'ar', name: 'العربية', flag: '🇸🇦' }
  ];

  useEffect(() => {
    if (user) {
      const userData = {
        full_name: user.full_name || '',
        email: user.email || '',
        preferred_language: user.preferred_language || 'en'
      };
      setFormData(userData);
      setOriginalData(userData);
    }
  }, [user]);

  const handleInputChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: value
    }));
  };

  const handleSaveProfile = async (e) => {
    e.preventDefault();
    
    if (!formData.full_name.trim()) {
      addNotification('Full name is required', 'error');
      return;
    }
    
    if (!formData.email.trim()) {
      addNotification('Email is required', 'error');
      return;
    }

    // Email validation
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailRegex.test(formData.email)) {
      addNotification('Please enter a valid email address', 'error');
      return;
    }

    setIsLoading(true);

    try {
      // Simulate API call
      await new Promise(resolve => setTimeout(resolve, 1000));
      
      await updateUserProfile(formData);
      
      // Switch language if changed
      if (formData.preferred_language !== currentLanguage) {
        switchLanguage(formData.preferred_language);
      }
      
      setOriginalData(formData);
      setIsEditing(false);
      addNotification('Profile updated successfully', 'success');
      
    } catch (error) {
      console.error('Profile update error:', error);
      addNotification('Failed to update profile', 'error');
    } finally {
      setIsLoading(false);
    }
  };

  const handleCancel = () => {
    setFormData(originalData);
    setIsEditing(false);
  };

  const handleLogout = async () => {
    if (window.confirm('Are you sure you want to logout?')) {
      try {
        await logout();
        addNotification('Logged out successfully', 'success');
      } catch (error) {
        console.error('Logout error:', error);
        addNotification('Logout failed', 'error');
      }
    }
  };

  const getLanguageName = (code) => {
    const lang = languages.find(l => l.code === code);
    return lang ? lang.name : 'English';
  };

  if (!user) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100 flex items-center justify-center">
        <div className="text-center">
          <h2 className="text-2xl font-bold text-gray-800 mb-4">Please log in to view your profile</h2>
          <button
            onClick={() => window.location.href = '/'}
            className="bg-blue-600 text-white px-6 py-3 rounded-lg hover:bg-blue-700 transition-colors duration-200"
          >
            Go to Home
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100 py-8">
      <div className="container mx-auto px-4">
        <div className="max-w-2xl mx-auto">
          {/* Header */}
          <div className="text-center mb-8">
            <div className="flex items-center justify-center mb-4">
              <div className="bg-blue-600 p-3 rounded-full mr-4">
                <svg className="w-8 h-8 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                </svg>
              </div>
              <h1 className="text-4xl font-bold text-gray-800">User Profile</h1>
            </div>
            <p className="text-gray-600 text-lg">
              Manage your account settings and preferences
            </p>
          </div>

          {/* Profile Form */}
          <div className="bg-white rounded-xl shadow-lg p-8 mb-6">
            <form onSubmit={handleSaveProfile} className="space-y-6">
              {/* Full Name */}
              <div>
                <label htmlFor="full_name" className="block text-sm font-medium text-gray-700 mb-2">
                  Full Name
                </label>
                <input
                  type="text"
                  id="full_name"
                  name="full_name"
                  value={formData.full_name}
                  onChange={handleInputChange}
                  placeholder="Enter your full name"
                  className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all duration-200 disabled:bg-gray-50"
                  disabled={!isEditing || isLoading}
                />
              </div>

              {/* Email */}
              <div>
                <label htmlFor="email" className="block text-sm font-medium text-gray-700 mb-2">
                  Email Address
                </label>
                <input
                  type="email"
                  id="email"
                  name="email"
                  value={formData.email}
                  onChange={handleInputChange}
                  placeholder="Enter your email address"
                  className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all duration-200 disabled:bg-gray-50"
                  disabled={!isEditing || isLoading}
                />
              </div>

              {/* Preferred Language */}
              <div>
                <label htmlFor="preferred_language" className="block text-sm font-medium text-gray-700 mb-2">
                  Preferred Language
                </label>
                <select
                  id="preferred_language"
                  name="preferred_language"
                  value={formData.preferred_language}
                  onChange={handleInputChange}
                  className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all duration-200 disabled:bg-gray-50"
                  disabled={!isEditing || isLoading}
                >
                  {languages.map(lang => (
                    <option key={lang.code} value={lang.code}>
                      {lang.flag} {lang.name}
                    </option>
                  ))}
                </select>
              </div>

              {/* Action Buttons */}
              <div className="flex gap-4">
                {!isEditing ? (
                  <button
                    type="button"
                    onClick={() => setIsEditing(true)}
                    className="flex-1 bg-blue-600 text-white py-3 px-6 rounded-lg hover:bg-blue-700 transition-colors duration-200 font-medium flex items-center justify-center"
                  >
                    <svg className="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                    </svg>
                    Edit Profile
                  </button>
                ) : (
                  <>
                    <button
                      type="submit"
                      disabled={isLoading}
                      className="flex-1 bg-green-600 text-white py-3 px-6 rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-200 font-medium flex items-center justify-center"
                    >
                      {isLoading ? (
                        <LoadingSpinner size="sm" />
                      ) : (
                        <>
                          <svg className="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                          </svg>
                          Save Changes
                        </>
                      )}
                    </button>
                    
                    <button
                      type="button"
                      onClick={handleCancel}
                      disabled={isLoading}
                      className="px-6 py-3 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-200 font-medium"
                    >
                      Cancel
                    </button>
                  </>
                )}
              </div>
            </form>
          </div>

          {/* Account Actions */}
          <div className="bg-white rounded-xl shadow-lg p-6">
            <h3 className="text-lg font-semibold text-gray-800 mb-4">Account Actions</h3>
            <div className="space-y-3">
              <button className="w-full p-4 border border-gray-200 rounded-lg hover:bg-gray-50 transition-colors duration-200 text-left flex items-center">
                <div className="bg-blue-100 p-2 rounded-full mr-3">
                  <svg className="w-5 h-5 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                  </svg>
                </div>
                <div>
                  <p className="font-medium text-gray-800">Change Password</p>
                  <p className="text-sm text-gray-600">Update your account password</p>
                </div>
              </button>

              <button className="w-full p-4 border border-gray-200 rounded-lg hover:bg-gray-50 transition-colors duration-200 text-left flex items-center">
                <div className="bg-green-100 p-2 rounded-full mr-3">
                  <svg className="w-5 h-5 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                </div>
                <div>
                  <p className="font-medium text-gray-800">Download Data</p>
                  <p className="text-sm text-gray-600">Export your account data</p>
                </div>
              </button>

              <button
                onClick={handleLogout}
                className="w-full p-4 border border-red-200 rounded-lg hover:bg-red-50 transition-colors duration-200 text-left flex items-center"
              >
                <div className="bg-red-100 p-2 rounded-full mr-3">
                  <svg className="w-5 h-5 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                  </svg>
                </div>
                <div>
                  <p className="font-medium text-red-800">Logout</p>
                  <p className="text-sm text-red-600">Sign out of your account</p>
                </div>
              </button>
            </div>
          </div>

          {/* Profile Stats */}
          <div className="mt-8 bg-gradient-to-r from-blue-600 to-purple-600 rounded-xl p-6 text-white">
            <h3 className="text-lg font-semibold mb-4">Account Summary</h3>
            <div className="grid grid-cols-2 gap-4">
              <div className="text-center">
                <p className="text-2xl font-bold">5</p>
                <p className="text-sm opacity-90">Active Bookings</p>
              </div>
              <div className="text-center">
                <p className="text-2xl font-bold">12</p>
                <p className="text-sm opacity-90">Total Orders</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default Profile;