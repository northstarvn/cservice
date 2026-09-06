import React, { createContext, useContext, useState, useEffect } from "react";
import { useAuth } from './AuthContext';
import { useApp } from './AppContext';
import { chatApi } from '../utils/api';

export const ChatContext = createContext();

export function useChat() {
  return useContext(ChatContext);
}

export function ChatProvider({ children }) {
  const [messages, setMessages] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [suggestions, setSuggestions] = useState([]);
  const [sentiment, setSentiment] = useState(null);
  const [currentLanguage, setCurrentLanguage] = useState("en");
  const [isVoiceMode, setIsVoiceMode] = useState(false);

  const { isAuthenticated, user } = useAuth();
  const { notify } = useApp();

  useEffect(() => {
    // Load history after login
    if (isAuthenticated && user) {
        const sessionId = user.id || user.username;
        chatApi.getChatHistory(sessionId)
        .then(data => {
            const historyItems = Array.isArray(data) ? data : data.items || data.history || [];
            setMessages(historyItems.map(h => ({
              text: h.response || h.text || h.message || "",
            isUser: false,
            timestamp: h.timestamp,
              userMessage: h.message || h.userMessage
          })));
        })
          .catch(() => notify("Failed to load chat history"));
    }
        }, [isAuthenticated, user, notify]);

  async function sendMessage(text) {
    if (!isAuthenticated) {
      notify("Please login to use chat.");
      return;
    }
    setIsLoading(true);
    setMessages((prev) => [
      ...prev,
      { text, isUser: true, timestamp: Date.now() }
    ]);
    try {
      const sessionId = user.id || user.username;
      const data = await chatApi.sendMessage(sessionId, text, currentLanguage);
      setMessages((prev) => [
        ...prev,
        {
            text: data.text || data.response || data.message || "",
          isUser: false,
          timestamp: Date.now(),
          sentiment: data.sentiment,
          vector: data.vector,
        }
      ]);
      setSuggestions(data.suggestions || []);
      setSentiment(data.sentiment);
    } catch (e) {
      notify("Error getting response.");
      setMessages((prev) => [
        ...prev,
        { text: "Error getting response.", isUser: false, timestamp: Date.now() }
      ]);
    }
    setIsLoading(false);
  }

  function switchLanguage(lang) {
    setCurrentLanguage(lang);
  }

  function toggleVoiceMode() {
    setIsVoiceMode((v) => !v);
  }

  async function startVoiceRecognition() {
    return new Promise((resolve, reject) => {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) return reject("Speech Recognition not supported.");
      const recognition = new SpeechRecognition();
      recognition.lang = currentLanguage;
      recognition.onresult = (event) => {
        resolve(event.results[0][0].transcript);
      };
      recognition.onerror = (event) => reject(event.error);
      recognition.start();
    });
  }

  return (
    <ChatContext.Provider value={{
      messages, isLoading, suggestions, sentiment,
      sendMessage, switchLanguage, startVoiceRecognition,
      isVoiceMode, toggleVoiceMode, currentLanguage
    }}>
      {children}
    </ChatContext.Provider>
  );
}