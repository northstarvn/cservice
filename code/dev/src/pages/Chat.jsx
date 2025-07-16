// src/pages/Chat.js
import React, { useState, useEffect, useRef } from 'react';
import { useAuth } from '../context/AuthContext';
import { useChat } from '../context/ChatContext';
import { useApp } from '../context/AppContext';
import LoadingSpinner from '../components/LoadingSpinner';

const Chat = () => {
  const { user } = useAuth();
  const { 
    messages, 
    isLoading, 
    currentLanguage, 
    suggestions, 
    sentiment,
    sendMessage, 
    switchLanguage, 
    startVoiceRecognition,
    isVoiceMode,
    toggleVoiceMode 
  } = useChat();
  const { isMobile } = useApp();
  
  const [inputMessage, setInputMessage] = useState('');
  const [isRecording, setIsRecording] = useState(false);
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  const languages = [
    { code: 'en', name: 'English', flag: '🇺🇸' },
    { code: 'es', name: 'Español', flag: '🇪🇸' },
    { code: 'fr', name: 'Français', flag: '🇫🇷' },
    { code: 'ar', name: 'العربية', flag: '🇸🇦' }
  ];

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  useEffect(() => {
    // Focus input when component mounts
    if (inputRef.current) {
      inputRef.current.focus();
    }
  }, []);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const handleSendMessage = async (e) => {
    e.preventDefault();
    if (!inputMessage.trim()) return;

    await sendMessage(inputMessage);
    setInputMessage('');
  };

  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage(e);
    }
  };

  const handleVoiceInput = async () => {
    if (!isRecording) {
      setIsRecording(true);
      try {
        const transcribedText = await startVoiceRecognition();
        if (transcribedText) {
          setInputMessage(transcribedText);
        }
      } catch (error) {
        console.error('Voice recognition error:', error);
      } finally {
        setIsRecording(false);
      }
    }
  };

  const handleLanguageSwitch = (langCode) => {
    switchLanguage(langCode);
  };

  const formatTimestamp = (timestamp) => {
    return new Date(timestamp).toLocaleTimeString([], { 
      hour: '2-digit', 
      minute: '2-digit' 
    });
  };

  const getSentimentColor = (sentiment) => {
    if (!sentiment) return '';
    if (sentiment.label === 'positive') return 'text-green-600';
    if (sentiment.label === 'negative') return 'text-red-600';
    return 'text-yellow-600';
  };

  const getSentimentIcon = (sentiment) => {
    if (!sentiment) return '';
    if (sentiment.label === 'positive') return '😊';
    if (sentiment.label === 'negative') return '😔';
    return '😐';
  };

  const currentLang = languages.find(lang => lang.code === currentLanguage) || languages[0];
  const isRTL = currentLanguage === 'ar';

  return (
    <div className={`flex flex-col h-screen bg-gray-50 ${isRTL ? 'rtl' : 'ltr'}`}>
      {/* Header */}
      <div className="bg-white shadow-sm border-b px-4 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 bg-blue-500 rounded-full flex items-center justify-center">
              <span className="text-white font-semibold">AI</span>
            </div>
            <div>
              <h1 className="text-lg font-semibold text-gray-900">
                AI Assistant
              </h1>
              <p className="text-sm text-gray-500">
                {isLoading ? 'Typing...' : 'Online'}
              </p>
            </div>
          </div>
          
          <div className="flex items-center space-x-2">
            {/* Language Selector */}
            <div className="relative">
              <select
                value={currentLanguage}
                onChange={(e) => handleLanguageSwitch(e.target.value)}
                className="bg-gray-100 border border-gray-300 rounded-md px-3 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                {languages.map(lang => (
                  <option key={lang.code} value={lang.code}>
                    {lang.flag} {lang.name}
                  </option>
                ))}
              </select>
            </div>

            {/* Voice Mode Toggle (Mobile) */}
            {isMobile && (
              <button
                onClick={toggleVoiceMode}
                className={`p-2 rounded-full transition-colors ${
                  isVoiceMode 
                    ? 'bg-blue-500 text-white' 
                    : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
                }`}
                title="Toggle Voice Mode"
              >
                🎤
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Chat Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 ? (
          <div className="text-center py-8">
            <div className="w-16 h-16 bg-blue-100 rounded-full flex items-center justify-center mx-auto mb-4">
              <span className="text-2xl">💬</span>
            </div>
            <h3 className="text-lg font-medium text-gray-900 mb-2">
              Welcome to AI Chat
            </h3>
            <p className="text-gray-600">
              Start a conversation and I'll help you with any questions or tasks.
            </p>
          </div>
        ) : (
          messages.map((message, index) => (
            <div
              key={index}
              className={`flex ${message.isUser ? 'justify-end' : 'justify-start'}`}
            >
              <div
                className={`max-w-xs lg:max-w-md px-4 py-2 rounded-lg ${
                  message.isUser
                    ? 'bg-blue-500 text-white'
                    : 'bg-white text-gray-900 shadow-sm border'
                }`}
              >
                <p className="text-sm">{message.text}</p>
                <div className="flex items-center justify-between mt-1">
                  <span className={`text-xs ${
                    message.isUser ? 'text-blue-100' : 'text-gray-500'
                  }`}>
                    {formatTimestamp(message.timestamp)}
                  </span>
                  {message.sentiment && !message.isUser && (
                    <span className={`text-xs ${getSentimentColor(message.sentiment)}`}>
                      {getSentimentIcon(message.sentiment)}
                    </span>
                  )}
                </div>
                {message.vector && (
                  <div className="mt-1 text-xs opacity-50">
                    Vector: [{message.vector.slice(0, 3).map(v => v.toFixed(2)).join(', ')}...]
                  </div>
                )}
              </div>
            </div>
          ))
        )}
        
        {isLoading && (
          <div className="flex justify-start">
            <div className="bg-white rounded-lg p-3 shadow-sm border">
              <LoadingSpinner size="sm" />
            </div>
          </div>
        )}
        
        <div ref={messagesEndRef} />
      </div>

      {/* AI Suggestions Panel */}
      {suggestions.length > 0 && (
        <div className="bg-yellow-50 border-t border-yellow-200 p-3">
          <div className="flex items-center space-x-2 mb-2">
            <span className="text-yellow-600 text-sm font-medium">💡 Suggestions:</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {suggestions.map((suggestion, index) => (
              <button
                key={index}
                onClick={() => setInputMessage(suggestion)}
                className="text-xs bg-yellow-100 hover:bg-yellow-200 text-yellow-800 px-2 py-1 rounded-full transition-colors"
              >
                {suggestion}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Input Area */}
      <div className="bg-white border-t p-4">
        <form onSubmit={handleSendMessage} className="flex items-end space-x-2">
          <div className="flex-1">
            <textarea
              ref={inputRef}
              value={inputMessage}
              onChange={(e) => setInputMessage(e.target.value)}
              onKeyPress={handleKeyPress}
              placeholder="Type your message..."
              className="w-full px-3 py-2 border border-gray-300 rounded-md resize-none focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              rows={1}
              style={{ minHeight: '38px', maxHeight: '120px' }}
              disabled={isLoading}
            />
          </div>
          
          {/* Voice Input Button (Mobile) */}
          {isMobile && (
            <button
              type="button"
              onClick={handleVoiceInput}
              disabled={isRecording || isLoading}
              className={`p-2 rounded-full transition-colors ${
                isRecording
                  ? 'bg-red-500 text-white animate-pulse'
                  : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
              }`}
              title="Voice Input"
            >
              🎤
            </button>
          )}
          
          {/* Send Button */}
          <button
            type="submit"
            disabled={!inputMessage.trim() || isLoading}
            className="bg-blue-500 text-white p-2 rounded-full hover:bg-blue-600 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
            title="Send Message"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
            </svg>
          </button>
        </form>
        
        {/* Voice Mode Indicator */}
        {isVoiceMode && (
          <div className="mt-2 text-center">
            <span className="text-xs text-blue-600 bg-blue-100 px-2 py-1 rounded-full">
              🎤 Voice Mode Active
            </span>
          </div>
        )}
      </div>
    </div>
  );
};

export default Chat;