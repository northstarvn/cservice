import React from 'react';
import { Home, MessageSquare, Calendar, Package, User, ClipboardList, Globe } from 'lucide-react';

const Navbar = ({ setCurrentScreen, language, setLanguage, setShowPopup }) => {
  const languages = ['en', 'es', 'fr', 'ar'];

  return (
    <nav className="bg-blue-900 text-white p-4">
      <div className="flex justify-between items-center">
        <div className="flex space-x-4">
          <button onClick={() => setCurrentScreen('home')}><Home /> Home</button>
          <button onClick={() => setCurrentScreen('chat')}><MessageSquare /> AI Chat</button>
          <button onClick={() => setCurrentScreen('booking')}><Calendar /> Book Services</button>
          <button onClick={() => setCurrentScreen('tracking')}><Package /> Track Delivery</button>
          <button onClick={() => setCurrentScreen('profile')}><User /> Profile</button>
          <button onClick={() => setCurrentScreen('planning')}><ClipboardList /> Project Planning</button>
        </div>
        <div className="flex space-x-2">
          <select value={language} onChange={(e) => setLanguage(e.target.value)}>
            {languages.map(lang => <option key={lang} value={lang}>{lang.toUpperCase()}</option>)}
          </select>
          <button onClick={() => setShowPopup('login')}>Login</button>
        </div>
      </div>
    </nav>
  );
};

export default Navbar;