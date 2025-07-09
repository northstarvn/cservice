```javascript
import React, { useState } from 'react';
import { i18next } from './i18n';

const saveProfile = async (userId, fullName, email, language) => {
  return true; // Mock response
};

const ProfileScreen = () => {
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [language, setLanguage] = useState('en');
  const [message, setMessage] = useState('');

  const handleSave = async () => {
    const result = await saveProfile('user123', fullName, email, language);
    if (result) {
      i18next.changeLanguage(language);
      document.documentElement.setAttribute('dir', language === 'ar' ? 'rtl' : 'ltr');
      setMessage(i18next.t('profile_saved'));
    } else {
      setMessage(i18next.t('profile_error'));
    }
  };

  return (
    <div className="p-8 max-w-md mx-auto">
      <h1 className="text-2xl font-bold mb-4">{i18next.t('profile')}</h1>
      <div className="flex flex-col gap-4">
        <input
          type="text"
          className="border rounded p-2"
          placeholder={i18next.t('full_name')}
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
        />
        <input
          type="email"
          className="border rounded p-2"
          placeholder={i18next.t('email')}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <select
          className="border rounded p-2"
          value={language}
          onChange={(e) => setLanguage(e.target.value)}
        >
          <option value="en">English</option>
          <option value="es">Español</option>
          <option value="fr">Français</option>
          <option value="ar">العربية</option>
        </select>
        <button
          onClick={handleSave}
          className="bg-blue-500 text-white px-4 py-2 rounded"
        >
          {i18next.t('save_profile')}
        </button>
        <button
          onClick={() => window.location.href = '/login'}
          className="bg-red-500 text-white px-4 py-2 rounded"
        >
          {i18next.t('logout')}
        </button>
        {message && <p className="text-sm text-green-600">{message}</p>}
      </div>
    </div>
  );
};

export default ProfileScreen;
```