import React, { useState } from "react";
import { useChat } from "../context/ChatContext";
import { useAuth } from '../context/AuthContext';
import { useApp } from '../context/AppContext';
import LoadingSpinner from '../components/LoadingSpinner';

export default function Chat() {
  const {
    messages,
    isLoading,
    suggestions,
    sentiment,
    sendMessage,
    switchLanguage,
    startVoiceRecognition,
    isVoiceMode,
    toggleVoiceMode,
    currentLanguage
  } = useChat();

  const { isAuthenticated, user } = useAuth();
  const { notify } = useApp();

  const [input, setInput] = useState("");

  const handleSend = async () => {
    if (!input.trim()) return;
    if (!isAuthenticated) {
      notify("Please log in to use the chat feature.");
      return;
    }
    await sendMessage(input);
    setInput("");
  };

  const handleSuggestion = async (suggestion) => {
    setInput(suggestion);
    await sendMessage(suggestion);
    setInput("");
  };

  const handleVoice = async () => {
    try {
      const text = await startVoiceRecognition();
      setInput(text);
      await sendMessage(text);
      setInput("");
    } catch (err) {
      notify("Voice recognition error: " + err);
    }
  };

  if (!isAuthenticated) {
    return (
      <div style={{ maxWidth: 600, margin: "auto", padding: 24, textAlign: "center" }}>
        <h2>AI Chat</h2>
        <p>Please log in to use the chat feature.</p>
      </div>
    );
  }

  return (
    <div className="chat-container" style={{ maxWidth: 600, margin: "auto", padding: 24 }}>
      <h2>AI Chat</h2>
      <div style={{ marginBottom: 12 }}>
        <span style={{ color: "#888", fontSize: 14 }}>
          {user && user.full_name ? `Logged in as: ${user.full_name}` : "Logged in"}
        </span>
      </div>
      <div className="chat-messages" style={{ border: "1px solid #eee", borderRadius: 8, padding: 16, minHeight: 300, background: "#fafafa" }}>
        {messages.map((msg, idx) => (
          <div key={idx} style={{ marginBottom: 12, textAlign: msg.isUser ? "right" : "left" }}>
            <div
              style={{
                display: "inline-block",
                padding: "8px 12px",
                borderRadius: 16,
                background: msg.isUser ? "#d1e7dd" : "#f8d7da"
              }}
            >
              {msg.text}
            </div>
            {msg.sentiment && (
              <span style={{ marginLeft: 8, fontSize: 12, color: "#888" }}>
                {msg.sentiment.label} ({(msg.sentiment.score * 100).toFixed(1)}%)
              </span>
            )}
          </div>
        ))}
        {isLoading && <LoadingSpinner />}
      </div>
      <div style={{ marginTop: 16 }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Type your message..."
          style={{ width: "60%", padding: 8, borderRadius: 8, border: "1px solid #ddd" }}
          onKeyDown={e => e.key === "Enter" ? handleSend() : null}
        />
        <button onClick={handleSend} style={{ marginLeft: 8, padding: "8px 16px", borderRadius: 8 }}>
          Send
        </button>
        <button onClick={toggleVoiceMode} style={{ marginLeft: 8, padding: "8px 16px", borderRadius: 8 }}>
          {isVoiceMode ? "Voice: On" : "Voice: Off"}
        </button>
        {isVoiceMode && (
          <button onClick={handleVoice} style={{ marginLeft: 8, padding: "8px 16px", borderRadius: 8 }}>
            Start Voice
          </button>
        )}
        <select
          value={currentLanguage}
          onChange={e => switchLanguage(e.target.value)}
          style={{ marginLeft: 8, padding: "8px 16px", borderRadius: 8 }}
        >
          <option value="en">English</option>
          <option value="fr">French</option>
          <option value="vi">Vietnamese</option>
        </select>
      </div>
      {suggestions.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <strong>Suggestions:</strong>
          <div>
            {suggestions.map((s, idx) => (
              <button key={idx} onClick={() => handleSuggestion(s)} style={{ margin: 4, padding: "6px 14px", borderRadius: 8, border: "1px solid #bbb" }}>
                {s}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}