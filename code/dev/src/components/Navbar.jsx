import React, { useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { useApp } from '../context/AppContext';
import { 
  Home, 
  MessageCircle, 
  Calendar, 
  Package, 
  User, 
  PlaneTakeoff,
  Menu,
  X,
  Globe
} from 'lucide-react';

const Navbar = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout } = useAuth();
  const { language, switchLanguage, translations } = useApp();
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  const [isLanguageMenuOpen, setIsLanguageMenuOpen] = useState(false);

  const menuItems = [
    { label: 'Home', route: '/', icon: Home },
    { label: 'AI Chat', route: '/chat', icon: MessageCircle },
    { label: 'Book Services', route: '/booking', icon: Calendar },
    { label: 'Track Delivery', route: '/tracking', icon: Package },
    { label: 'Profile', route: '/profile', icon: User },
    { label: 'Project Planning', route: '/planning', icon: PlaneTakeoff }
  ];

  const languages = [
    { code: 'en', name: 'English', flag: '🇺🇸' },
    { code: 'es', name: 'Español', flag: '🇪🇸' },
    { code: 'fr', name: 'Français', flag: '🇫🇷' },
    { code: 'ar', name: 'العربية', flag: '🇸🇦' }
  ];

  const handleNavigation = (route) => {
    navigate(route);
    setIsMobileMenuOpen(false);
  };

  const handleLanguageChange = (langCode) => {
    switchLanguage(langCode);
    setIsLanguageMenuOpen(false);
  };

  const handleLogout = () => {
    logout();
    navigate('/');
    setIsMobileMenuOpen(false);
  };

  const isActiveRoute = (route) => {
    return location.pathname === route;
  };

  return (
    <nav className={`bg-gradient-to-r from-blue-600 to-blue-800 shadow-lg sticky top-0 z-50 ${language === 'ar' ? 'rtl' : 'ltr'}`}>
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          {/* Logo */}
          <div className="flex items-center">
            <div className="flex-shrink-0">
              <h1 className="text-white text-xl font-bold">
                {translations?.brand || 'CustomerService'}
              </h1>
            </div>
          </div>

          {/* Desktop Menu */}
          <div className="hidden md:block">
            <div className="ml-10 flex items-baseline space-x-4">
              {menuItems.map((item) => {
                const Icon = item.icon;
                return (
                  <button
                    key={item.route}
                    onClick={() => handleNavigation(item.route)}
                    className={`px-3 py-2 rounded-md text-sm font-medium flex items-center space-x-2 transition-colors duration-200 ${
                      isActiveRoute(item.route)
                        ? 'bg-blue-700 text-white'
                        : 'text-blue-100 hover:bg-blue-700 hover:text-white'
                    }`}
                  >
                    <Icon size={16} />
                    <span>{translations?.[item.label.toLowerCase().replace(' ', '_')] || item.label}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Desktop Right Section */}
          <div className="hidden md:flex items-center space-x-4">
            {/* Language Selector */}
            <div className="relative">
              <button
                onClick={() => setIsLanguageMenuOpen(!isLanguageMenuOpen)}
                className="flex items-center space-x-2 px-3 py-2 rounded-md text-sm font-medium text-blue-100 hover:bg-blue-700 hover:text-white transition-colors duration-200"
              >
                <Globe size={16} />
                <span>{languages.find(lang => lang.code === language)?.flag}</span>
              </button>

              {isLanguageMenuOpen && (
                <div className="absolute right-0 mt-2 w-48 bg-white rounded-md shadow-lg py-1 z-50">
                  {languages.map((lang) => (
                    <button
                      key={lang.code}
                      onClick={() => handleLanguageChange(lang.code)}
                      className={`block w-full text-left px-4 py-2 text-sm hover:bg-gray-100 ${
                        language === lang.code ? 'bg-blue-50 text-blue-600' : 'text-gray-700'
                      }`}
                    >
                      <span className="mr-2">{lang.flag}</span>
                      {lang.name}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* User Menu */}
            {user ? (
              <div className="flex items-center space-x-2">
                <span className="text-blue-100 text-sm">
                  {translations?.welcome || 'Welcome'}, {user.name}
                </span>
                <button
                  onClick={handleLogout}
                  className="px-3 py-2 rounded-md text-sm font-medium text-blue-100 hover:bg-blue-700 hover:text-white transition-colors duration-200"
                >
                  {translations?.logout || 'Logout'}
                </button>
              </div>
            ) : (
              <button
                onClick={() => navigate('/login')}
                className="px-3 py-2 rounded-md text-sm font-medium text-blue-100 hover:bg-blue-700 hover:text-white transition-colors duration-200"
              >
                {translations?.login || 'Login'}
              </button>
            )}
          </div>

          {/* Mobile menu button */}
          <div className="md:hidden">
            <button
              onClick={() => setIsMobileMenuOpen(!isMobileMenuOpen)}
              className="inline-flex items-center justify-center p-2 rounded-md text-blue-100 hover:text-white hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-white"
            >
              {isMobileMenuOpen ? <X size={24} /> : <Menu size={24} />}
            </button>
          </div>
        </div>
      </div>

      {/* Mobile Menu */}
      {isMobileMenuOpen && (
        <div className="md:hidden bg-blue-700">
          <div className="px-2 pt-2 pb-3 space-y-1 sm:px-3">
            {menuItems.map((item) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.route}
                  onClick={() => handleNavigation(item.route)}
                  className={`block w-full text-left px-3 py-2 rounded-md text-base font-medium flex items-center space-x-2 transition-colors duration-200 ${
                    isActiveRoute(item.route)
                      ? 'bg-blue-800 text-white'
                      : 'text-blue-100 hover:bg-blue-800 hover:text-white'
                  }`}
                >
                  <Icon size={20} />
                  <span>{translations?.[item.label.toLowerCase().replace(' ', '_')] || item.label}</span>
                </button>
              );
            })}
          </div>

          {/* Mobile Language & User Section */}
          <div className="pt-4 pb-3 border-t border-blue-600">
            <div className="flex items-center px-5 space-x-3">
              <div className="flex-1">
                <select
                  value={language}
                  onChange={(e) => handleLanguageChange(e.target.value)}
                  className="w-full px-3 py-2 text-sm bg-blue-800 text-white rounded-md border border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-400"
                >
                  {languages.map((lang) => (
                    <option key={lang.code} value={lang.code}>
                      {lang.flag} {lang.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="mt-3 px-2 space-y-1">
              {user ? (
                <div>
                  <div className="px-3 py-2 text-blue-100 text-sm">
                    {translations?.welcome || 'Welcome'}, {user.name}
                  </div>
                  <button
                    onClick={handleLogout}
                    className="block w-full text-left px-3 py-2 rounded-md text-base font-medium text-blue-100 hover:bg-blue-800 hover:text-white transition-colors duration-200"
                  >
                    {translations?.logout || 'Logout'}
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => navigate('/login')}
                  className="block w-full text-left px-3 py-2 rounded-md text-base font-medium text-blue-100 hover:bg-blue-800 hover:text-white transition-colors duration-200"
                >
                  {translations?.login || 'Login'}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </nav>
  );
};

export default Navbar;