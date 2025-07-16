import React from 'react';

const ChatScreen = () => {
  return (
    <div className="p-6">
      <h2>AI Assistant</h2>
      <textarea placeholder="Type your message..."></textarea>
      <button onClick={() => alert('Message sent')}>Send</button>
    </div>
  );
};

export default ChatScreen;