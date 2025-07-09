import React from 'react';

const HomeScreen = () => {
  return (
    <div className="p-6 text-center">
      <h1>Welcome to Customer Service</h1>
      <p>Support at Your Fingertips</p>
      <button onClick={() => alert('Chat opened')}>Start AI Chat</button>
    </div>
  );
};

export default HomeScreen;