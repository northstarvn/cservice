// src/context/ChatContext.js
import React, { createContext, useContext, useState, useEffect } from 'react';
import { useAuth } from './AuthContext';

const ChatContext = createContext();

export const useChat = () => {
  const context = useContext(ChatContext);
  if (!context) {
    console.error('useChat must be used within a ChatProvider');
    throw new Error('useChat must be used within a ChatProvider');
  }
  return context;
};
export const ChatProvider = ({ children }) => {
  const { user } = useAuth();
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [voiceMode, setVoiceMode] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [language, setLanguage] = useState('en');
  const [aiSuggestions, setAiSuggestions] = useState([]);

  // Initialize chat session
  useEffect(() => {
    if (user) {
      initializeChat();
    }
  }, [user]);

  const initializeChat = async () => {
    try {
      setLoading(true);
      
      // Mock session initialization
      const mockSessionId = 'session_' + Date.now();
      setSessionId(mockSessionId);
      
      // Add welcome message
      const welcomeMessage = {
        id: 'msg_welcome',
        type: 'ai',
        content: 'Hello! I\'m your AI assistant. How can I help you today?',
        timestamp: new Date().toISOString(),
        vector: [0.1, 0.2, 0.3], // Mock vector
        sentiment: { score: 0.8, label: 'positive' }
      };
      
      setMessages([welcomeMessage]);
      
      // Generate initial suggestions
      setAiSuggestions([
        'Track my delivery',
        'Book a service',
        'Check my profile',
        'Get help with booking'
      ]);
      
    } catch (error) {
      console.error('Error initializing chat:', error);
    } finally {
      setLoading(false);
    }
  };

  const sendMessage = async (content, type = 'user') => {
    if (!content.trim()) return;

    const userMessage = {
      id: 'msg_' + Date.now(),
      type: 'user',
      content: content.trim(),
      timestamp: new Date().toISOString(),
      vector: generateMockVector(),
      sentiment: await analyzeSentiment(content)
    };

    setMessages(prev => [...prev, userMessage]);
    setLoading(true);

    try {
      // Mock AI response with vector search
      const aiResponse = await generateAIResponse(content);
      
      const aiMessage = {
        id: 'msg_ai_' + Date.now(),
        type: 'ai',
        content: aiResponse.response,
        timestamp: new Date().toISOString(),
        vector: aiResponse.vector,
        sentiment: { score: 0.5, label: 'neutral' }
      };

      setMessages(prev => [...prev, aiMessage]);
      
      // Update suggestions based on context
      updateAISuggestions(content);
      
    } catch (error) {
      console.error('Error sending message:', error);
      
      const errorMessage = {
        id: 'msg_error_' + Date.now(),
        type: 'ai',
        content: 'Sorry, I encountered an error. Please try again.',
        timestamp: new Date().toISOString(),
        vector: [0, 0, 0],
        sentiment: { score: -0.2, label: 'negative' }
      };
      
      setMessages(prev => [...prev, errorMessage]);
    } finally {
      setLoading(false);
    }
  };

  const generateAIResponse = async (userMessage) => {
    // Mock AI response generation with vector search
    await new Promise(resolve => setTimeout(resolve, 1000));
    
    const lowerMessage = userMessage.toLowerCase();
    
    let response;
    if (lowerMessage.includes('track') || lowerMessage.includes('delivery')) {
      response = 'I can help you track your delivery. Please provide your tracking ID, or you can navigate to the tracking page.';
    } else if (lowerMessage.includes('book') || lowerMessage.includes('service')) {
      response = 'I can help you book a service. What type of service would you like to book - Consulting, Delivery, or Meeting?';
    } else if (lowerMessage.includes('profile')) {
      response = 'You can view and update your profile information in the Profile section. Would you like me to guide you there?';
    } else if (lowerMessage.includes('hello') || lowerMessage.includes('hi')) {
      response = 'Hello! How can I assist you today?';
    } else {
      response = 'I understand you need help. Could you please provide more details about what you\'re looking for?';
    }
    
    return {
      response,
      vector: generateMockVector()
    };
  };

  const analyzeSentiment = async (message) => {
    // Mock sentiment analysis
    const positiveWords = ['good', 'great', 'excellent', 'thank', 'thanks', 'awesome', 'wonderful'];
    const negativeWords = ['bad', 'terrible', 'awful', 'hate', 'upset', 'angry', 'frustrated'];
    
    const words = message.toLowerCase().split(' ');
    let score = 0;
    
    words.forEach(word => {
      if (positiveWords.includes(word)) score += 0.3;
      if (negativeWords.includes(word)) score -= 0.3;
    });
    
    score = Math.max(-1, Math.min(1, score));
    
    let label;
    if (score > 0.1) label = 'positive';
    else if (score < -0.1) label = 'negative';
    else label = 'neutral';
    
    return { score, label };
  };

  const updateAISuggestions = (userMessage) => {
    const lowerMessage = userMessage.toLowerCase();
    
    if (lowerMessage.includes('track')) {
      setAiSuggestions([
        'Check delivery status',
        'Get tracking updates',
        'Contact delivery support',
        'View delivery history'
      ]);
    } else if (lowerMessage.includes('book')) {
      setAiSuggestions([
        'Book consulting service',
        'Schedule a meeting',
        'Book delivery service',
        'View available times'
      ]);
    } else {
      setAiSuggestions([
        'Track my delivery',
        'Book a service',
        'Check my profile',
        'Get help with booking'
      ]);
    }
  };

  const generateMockVector = () => {
    return Array.from({ length: 128 }, () => Math.random() * 2 - 1);
  };

  const startVoiceRecognition = () => {
    if (!('webkitSpeechRecognition' in window)) {
      alert('Speech recognition not supported in this browser');
      return;
    }

    const recognition = new window.webkitSpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = language;

    recognition.onstart = () => {
      setIsListening(true);
    };

    recognition.onresult = (event) => {
      const transcript = event.results[0][0].transcript;
      sendMessage(transcript);
    };

    recognition.onerror = (event) => {
      console.error('Speech recognition error:', event.error);
      setIsListening(false);
    };

    recognition.onend = () => {
      setIsListening(false);
    };

    recognition.start();
  };

  const toggleVoiceMode = () => {
    setVoiceMode(!voiceMode);
  };

  const switchLanguage = (newLanguage) => {
    setLanguage(newLanguage);
    // In real app, would reload translations and update AI responses
  };

  const clearChat = () => {
    setMessages([]);
    setAiSuggestions([]);
    initializeChat();
  };

  const value = {
    messages,
    loading,
    sessionId,
    voiceMode,
    isListening,
    language,
    aiSuggestions,
    sendMessage,
    startVoiceRecognition,
    toggleVoiceMode,
    switchLanguage,
    clearChat
  };

  return (
    <ChatContext.Provider value={value}>
      {children}
    </ChatContext.Provider>
  );
};