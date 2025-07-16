import React from 'react';

const Popup = ({ type, onClose }) => {
  if (type === 'login') {
    return (
      <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center">
        <div className="bg-white p-6 rounded">
          <h3>Login</h3>
          <input type="text" placeholder="Username" />
          <input type="password" placeholder="Password" />
          <button onClick={onClose}>Login</button>
          <button onClick={onClose}>Cancel</button>
        </div>
      </div>
    );
  }
  return null;
};

export default Popup;