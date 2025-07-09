const { useState, useEffect } = React;

const Navbar = () => (
  <nav className="bg-blue-900 text-white p-4">
    <ul className="flex space-x-6">
      <li><a href="/" className="hover:text-blue-300">Home</a></li>
      <li><a href="/chat" className="hover:text-blue-300">AI Chat</a></li>
      <li><a href="/booking" className="hover:text-blue-300">Book Services</a></li>
      <li><a href="/tracking" className="hover:text-blue-300">Track Delivery</a></li>
      <li><a href="/profile" className="hover:text-blue-300">Profile</a></li>
      <li><a href="/planning" className="hover:text-blue-300">Project Planning</a></li>
    </ul>
  </nav>
);

const HomeScreen = () => (
  <div className="text-center p-6 bg-gray-100">
    <h1 className="text-3xl font-bold">Welcome to Customer Service</h1>
    <p className="mt-4">Support at Your Fingertips</p>
    <button className="mt-4 bg-blue-500 text-white p-2 rounded" onClick={() => alert('Opening chat')}>Start AI Chat</button>
  </div>
);

const ChatScreen = () => {
  const [message, setMessage] = useState('');
  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold">AI Assistant</h1>
      <textarea className="w-full p-2 border rounded mt-4" placeholder="Type your message..." value={message} onChange={(e) => setMessage(e.target.value)} />
      <button className="mt-2 bg-blue-500 text-white p-2 rounded" onClick={() => alert('Message sent')}>Send</button>
      <button className="mt-2 ml-2 bg-gray-500 text-white p-2 rounded" onClick={() => alert('Language switched')}>Switch Language</button>
    </div>
  );
};

const BookingScreen = () => {
  const [serviceType, setServiceType] = useState('Consulting');
  const [date, setDate] = useState('');
  const [time, setTime] = useState('');
  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold">Book Services or Meetings</h1>
      <select className="w-full p-2 border rounded mt-4" value={serviceType} onChange={(e) => setServiceType(e.target.value)}>
        <option>Consulting</option>
        <option>Delivery</option>
        <option>Meeting</option>
      </select>
      <input type="date" className="w-full p-2 border rounded mt-4" value={date} onChange={(e) => setDate(e.target.value)} />
      <input type="time" className="w-full p-2 border rounded mt-4" value={time} onChange={(e) => setTime(e.target.value)} />
      <button className="mt-2 bg-blue-500 text-white p-2 rounded" onClick={() => alert('Booking submitted')}>Submit Booking</button>
      <button className="mt-2 ml-2 bg-gray-500 text-white p-2 rounded" onClick={() => alert('Booking cancelled')}>Cancel</button>
    </div>
  );
};

const TrackingScreen = () => {
  const [trackingId, setTrackingId] = useState('');
  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold">Track Delivery</h1>
      <input type="text" className="w-full p-2 border rounded mt-4" placeholder="Enter tracking ID" value={trackingId} onChange={(e) => setTrackingId(e.target.value)} />
      <button className="mt-2 bg-blue-500 text-white p-2 rounded" onClick={() => alert('Tracking: In Transit')}>Track</button>
    </div>
  );
};

const ProfileScreen = () => {
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [language, setLanguage] = useState('en');
  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold">User Profile</h1>
      <input type="text" className="w-full p-2 border rounded mt-4" placeholder="Enter full name" value={fullName} onChange={(e) => setFullName(e.target.value)} />
      <input type="email" className="w-full p-2 border rounded mt-4" placeholder="Enter email" value={email} onChange={(e) => setEmail(e.target.value)} />
      <select className="w-full p-2 border rounded mt-4" value={language} onChange={(e) => setLanguage(e.target.value)}>
        <option value="en">English</option>
        <option value="es">Spanish</option>
        <option value="fr">French</option>
        <option value="ar">Arabic</option>
      </select>
      <button className="mt-2 bg-blue-500 text-white p-2 rounded" onClick={() => alert('Profile saved')}>Save Profile</button>
      <button className="mt-2 ml-2 bg-gray-500 text-white p-2 rounded" onClick={() => alert('Logged out')}>Logout</button>
    </div>
  );
};

const PlanningScreen = () => {
  const [projectName, setProjectName] = useState('');
  const [requirements, setRequirements] = useState('');
  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold">Project Planning</h1>
      <input type="text" className="w-full p-2 border rounded mt-4" placeholder="Enter project name" value={projectName} onChange={(e) => setProjectName(e.target.value)} />
      <textarea className="w-full p-2 border rounded mt-4" placeholder="Describe project requirements" value={requirements} onChange={(e) => setRequirements(e.target.value)} />
      <button className="mt-2 bg-blue-500 text-white p-2 rounded" onClick={() => alert('Plan submitted')}>Submit Plan</button>
      <button className="mt-2 ml-2 bg-gray-500 text-white p-2 rounded" onClick={() => alert('AI suggestions generated')}>AI Suggest</button>
    </div>
  );
};

const LoginPopup = ({ onClose }) => (
  <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center">
    <div className="bg-white p-6 rounded shadow-lg">
      <h2 className="text-xl font-bold">Login</h2>
      <input type="text" className="w-full p-2 border rounded mt-4" placeholder="Enter username" />
      <input type="password" className="w-full p-2 border rounded mt-4" placeholder="Enter password" />
      <button className="mt-2 bg-blue-500 text-white p-2 rounded" onClick={() => alert('Logged in')}>Login</button>
      <button className="mt-2 ml-2 bg-gray-500 text-white p-2 rounded" onClick={onClose}>Cancel</button>
    </div>
  </div>
);

const BookingConfirmationPopup = ({ onClose }) => (
  <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center">
    <div className="bg-white p-6 rounded shadow-lg">
      <h2 className="text-xl font-bold">Booking Confirmation</h2>
      <p className="mt-4">Booking confirmed! Details sent to your email.</p>
      <button className="mt-2 bg-blue-500 text-white p-2 rounded" onClick={onClose}>OK</button>
    </div>
  </div>
);

const TrackingResultPopup = ({ onClose, status }) => (
  <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center">
    <div className="bg-white p-6 rounded shadow-lg">
      <h2 className="text-xl font-bold">Delivery Status</h2>
      <p className="mt-4">Delivery status: {status}</p>
      <button className="mt-2 bg-blue-500 text-white p-2 rounded" onClick={onClose}>OK</button>
    </div>
  </div>
);

const App = () => {
  const [screen, setScreen] = useState('home');
  const [showLogin, setShowLogin] = useState(false);
  const [showBookingConfirmation, setShowBookingConfirmation] = useState(false);
  const [showTrackingResult, setShowTrackingResult] = useState(false);
  const [trackingStatus, setTrackingStatus] = useState('');

  useEffect(() => {
    const handleRoute = () => {
      const path = window.location.pathname;
      if (path === '/chat') setScreen('chat');
      else if (path === '/booking') setScreen('booking');
      else if (path === '/tracking') setScreen('tracking');
      else if (path === '/profile') setScreen('profile');
      else if (path === '/planning') setScreen('planning');
      else setScreen('home');
    };
    window.addEventListener('popstate', handleRoute);
    handleRoute();
    return () => window.removeEventListener('popstate', handleRoute);
  }, []);

  return (
    <div>
      <Navbar />
      {screen === 'home' && <HomeScreen />}
      {screen === 'chat' && <ChatScreen />}
      {screen === 'booking' && <BookingScreen />}
      {screen === 'tracking' && <TrackingScreen />}
      {screen === 'profile' && <ProfileScreen />}
      {screen === 'planning' && <PlanningScreen />}
      {showLogin && <LoginPopup onClose={() => setShowLogin(false)} />}
      {showBookingConfirmation && <BookingConfirmationPopup onClose={() => setShowBookingConfirmation(false)} />}
      {showTrackingResult && <TrackingResultPopup onClose={() => setShowTrackingResult(false)} status={trackingStatus} />}
    </div>
  );
};

ReactDOM.render(<App />, document.getElementById('root'));