import React from 'react';

const TrackingScreen = () => {
  return (
    <div className="p-6">
      <h2>Track Delivery</h2>
      <input type="text" placeholder="Enter tracking ID" />
      <button onClick={() => alert('Tracking: In Transit')}>Track</button>
    </div>
  );
};

export default TrackingScreen;