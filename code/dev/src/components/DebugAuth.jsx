// STEP 1: Add this debug component to see what's happening
// Create a new file: src/components/DebugAuth.jsx

import React from 'react';
import { useAuth } from '../context/AuthContext';
import { useApp } from '../context/AppContext';

const DebugAuth = () => {
  const auth = useAuth();
  const app = useApp();

  return (
    <div style={{
      position: 'fixed',
      top: '10px',
      right: '10px',
      background: 'black',
      color: 'white',
      padding: '10px',
      fontSize: '12px',
      zIndex: 9999,
      maxWidth: '300px'
    }}>
      <h3>DEBUG AUTH STATE:</h3>
      <div>Auth Loading: {auth.loading ? 'TRUE' : 'FALSE'}</div>
      <div>App Loading: {app.loading ? 'TRUE' : 'FALSE'}</div>
      <div>Is Authenticated: {auth.isAuthenticated ? 'TRUE' : 'FALSE'}</div>
      <div>User: {auth.user ? JSON.stringify(auth.user) : 'NULL'}</div>
      <div>Token: {localStorage.getItem('auth_token') ? 'EXISTS' : 'MISSING'}</div>
      <div>Show Login Popup: {app.showLoginPopup ? 'TRUE' : 'FALSE'}</div>
    </div>
  );
};

export default DebugAuth;
