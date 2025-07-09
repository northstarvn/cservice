import React, { useState, useEffect } from 'react';
import { MessageCircle, Home, Calendar, MapPin, User, FileText, Send, Mic, Globe, X, Check } from 'lucide-react';

const App = () => {
  const [currentScreen, setCurrentScreen] = useState('home');
  const [showLoginPopup, setShowLoginPopup] = useState(false);
  const [showBookingConfirmation, setShowBookingConfirmation] = useState(false);
  const [showTrackingResult, setShowTrackingResult] = useState(false);
  const [currentLanguage, setCurrentLanguage] = useState('en');
  const [isRTL, setIsRTL] = useState(false);
  const [user, setUser] = useState(null);
  const [chatMessages, setChatMessages] = useState([]);
  const [bookingData, setBookingData] = useState(null);
  const [trackingData, setTrackingData] = useState(null);

  // Language translations
  const translations = {
    en: {
      welcome: "Welcome to Customer Service",
      support: "Support at Your Fingertips",
      startChat: "Start AI Chat",
      home: "Home",
      aiChat: "AI Chat",
      bookServices: "Book Services",
      trackDelivery: "Track Delivery",
      profile: "Profile",
      projectPlanning: "Project Planning",
      login: "Login",
      username: "Username",
      password: "Password",
      cancel: "Cancel",
      bookingConfirmed: "Booking Confirmed!",
      detailsSent: "Details sent to your email.",
      ok: "OK",
      deliveryStatus: "Delivery Status",
      typeMessage: "Type your message...",
      send: "Send",
      voiceMode: "Voice Mode",
      switchLanguage: "Switch Language"
    },
    es: {
      welcome: "Bienvenido al Servicio al Cliente",
      support: "Soporte al Alcance de Tus Dedos",
      startChat: "Iniciar Chat AI",
      home: "Inicio",
      aiChat: "Chat AI",
      bookServices: "Reservar Servicios",
      trackDelivery: "Rastrear Entrega",
      profile: "Perfil",
      projectPlanning: "Planificación de Proyectos",
      login: "Iniciar Sesión",
      username: "Usuario",
      password: "Contraseña",
      cancel: "Cancelar",
      bookingConfirmed: "¡Reserva Confirmada!",
      detailsSent: "Detalles enviados a tu email.",
      ok: "OK",
      deliveryStatus: "Estado de Entrega",
      typeMessage: "Escribe tu mensaje...",
      send: "Enviar",
      voiceMode: "Modo de Voz",
      switchLanguage: "Cambiar Idioma"
    },
    fr: {
      welcome: "Bienvenue au Service Client",
      support: "Support à Portée de Main",
      startChat: "Démarrer Chat AI",
      home: "Accueil",
      aiChat: "Chat AI",
      bookServices: "Réserver Services",
      trackDelivery: "Suivre Livraison",
      profile: "Profil",
      projectPlanning: "Planification de Projet",
      login: "Connexion",
      username: "Nom d'utilisateur",
      password: "Mot de passe",
      cancel: "Annuler",
      bookingConfirmed: "Réservation Confirmée!",
      detailsSent: "Détails envoyés à votre email.",
      ok: "OK",
      deliveryStatus: "Statut de Livraison",
      typeMessage: "Tapez votre message...",
      send: "Envoyer",
      voiceMode: "Mode Vocal",
      switchLanguage: "Changer de Langue"
    },
    ar: {
      welcome: "مرحباً بك في خدمة العملاء",
      support: "الدعم في متناول يدك",
      startChat: "بدء الدردشة بالذكاء الاصطناعي",
      home: "الرئيسية",
      aiChat: "الدردشة بالذكاء الاصطناعي",
      bookServices: "حجز الخدمات",
      trackDelivery: "تتبع التسليم",
      profile: "الملف الشخصي",
      projectPlanning: "تخطيط المشاريع",
      login: "تسجيل الدخول",
      username: "اسم المستخدم",
      password: "كلمة المرور",
      cancel: "إلغاء",
      bookingConfirmed: "تم تأكيد الحجز!",
      detailsSent: "تم إرسال التفاصيل إلى بريدك الإلكتروني.",
      ok: "موافق",
      deliveryStatus: "حالة التسليم",
      typeMessage: "اكتب رسالتك...",
      send: "إرسال",
      voiceMode: "وضع الصوت",
      switchLanguage: "تغيير اللغة"
    }
  };

  const t = translations[currentLanguage];

  useEffect(() => {
    setIsRTL(currentLanguage === 'ar');
  }, [currentLanguage]);

  // Popup component definitions start here
  const LoginPopup = () => {
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');

    const handleLogin = () => {
      if (username && password) {
        setUser({ username, id: 'user123' });
        setShowLoginPopup(false);
        setUsername('');
        setPassword('');
      }
    };

    return (
      <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
        <div className={`bg-white rounded-lg p-6 w-96 max-w-md mx-4 ${isRTL ? 'text-right' : 'text-left'}`}>
          <div className="flex justify-between items-center mb-4">
            <h2 className="text-xl font-bold text-gray-800">{t.login}</h2>
            <button
              onClick={() => setShowLoginPopup(false)}
              className="text-gray-500 hover:text-gray-700"
            >
              <X size={20} />
            </button>
          </div>
          
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                {t.username}
              </label>
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full p-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                placeholder={t.username}
              />
            </div>
            
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                {t.password}
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full p-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                placeholder={t.password}
              />
            </div>
          </div>
          
          <div className="flex gap-3 mt-6">
            <button
              onClick={handleLogin}
              className="flex-1 bg-blue-600 text-white py-3 rounded-lg hover:bg-blue-700 transition-colors font-medium"
            >
              {t.login}
            </button>
            <button
              onClick={() => setShowLoginPopup(false)}
              className="flex-1 bg-gray-300 text-gray-700 py-3 rounded-lg hover:bg-gray-400 transition-colors font-medium"
            >
              {t.cancel}
            </button>
          </div>
        </div>
      </div>
    );
  };

  const BookingConfirmationPopup = () => {
    return (
      <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
        <div className={`bg-white rounded-lg p-6 w-96 max-w-md mx-4 ${isRTL ? 'text-right' : 'text-left'}`}>
          <div className="flex justify-between items-center mb-4">
            <h2 className="text-xl font-bold text-green-600">{t.bookingConfirmed}</h2>
            <div className="bg-green-100 p-2 rounded-full">
              <Check className="text-green-600" size={24} />
            </div>
          </div>
          
          <div className="mb-6">
            <p className="text-gray-700 mb-4">{t.detailsSent}</p>
            {bookingData && (
              <div className="bg-gray-50 p-4 rounded-lg">
                <div className="space-y-2 text-sm">
                  <p><strong>Service:</strong> {bookingData.serviceType}</p>
                  <p><strong>Date:</strong> {bookingData.date}</p>
                  <p><strong>Time:</strong> {bookingData.time}</p>
                  <p><strong>Booking ID:</strong> {bookingData.bookingId}</p>
                </div>
              </div>
            )}
          </div>
          
          <button
            onClick={() => setShowBookingConfirmation(false)}
            className="w-full bg-blue-600 text-white py-3 rounded-lg hover:bg-blue-700 transition-colors font-medium"
          >
            {t.ok}
          </button>
        </div>
      </div>
    );
  };

  const TrackingResultPopup = () => {
    return (
      <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
        <div className={`bg-white rounded-lg p-6 w-96 max-w-md mx-4 ${isRTL ? 'text-right' : 'text-left'}`}>
          <div className="flex justify-between items-center mb-4">
            <h2 className="text-xl font-bold text-gray-800">{t.deliveryStatus}</h2>
            <button
              onClick={() => setShowTrackingResult(false)}
              className="text-gray-500 hover:text-gray-700"
            >
              <X size={20} />
            </button>
          </div>
          
          <div className="mb-6">
            {trackingData ? (
              <div className="space-y-4">
                <div className="bg-blue-50 p-4 rounded-lg">
                  <div className="flex items-center gap-3 mb-2">
                    <MapPin className="text-blue-600" size={20} />
                    <span className="font-medium text-blue-800">
                      Status: {trackingData.status}
                    </span>
                  </div>
                  <p className="text-sm text-gray-600 mb-2">
                    Tracking ID: {trackingData.trackingId}
                  </p>
                  <p className="text-sm text-gray-600">
                    Last Updated: {trackingData.lastUpdated}
                  </p>
                </div>
                
                {trackingData.details && (
                  <div className="bg-gray-50 p-4 rounded-lg">
                    <h4 className="font-medium mb-2">Delivery Details:</h4>
                    <div className="space-y-1 text-sm text-gray-600">
                      <p>Expected Delivery: {trackingData.details.expectedDelivery}</p>
                      <p>Current Location: {trackingData.details.currentLocation}</p>
                      <p>Carrier: {trackingData.details.carrier}</p>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <p className="text-gray-600">No tracking information available.</p>
            )}
          </div>
          
          <button
            onClick={() => setShowTrackingResult(false)}
            className="w-full bg-blue-600 text-white py-3 rounded-lg hover:bg-blue-700 transition-colors font-medium"
          >
            {t.ok}
          </button>
        </div>
      </div>
    );
  };

  // Action handlers
  const handleBooking = (serviceType, date, time) => {
    const bookingId = `booking_${Date.now()}`;
    setBookingData({
      serviceType,
      date,
      time,
      bookingId
    });
    setShowBookingConfirmation(true);
  };

  const handleTracking = (trackingId) => {
    // Simulate tracking data
    setTrackingData({
      trackingId,
      status: "In Transit",
      lastUpdated: new Date().toLocaleDateString(),
      details: {
        expectedDelivery: "July 10, 2025",
        currentLocation: "Distribution Center - Hanoi",
        carrier: "Express Delivery Service"
      }
    });
    setShowTrackingResult(true);
  };

  const switchLanguage = (lang) => {
    setCurrentLanguage(lang);
  };

  return (
    <div className={`min-h-screen bg-gradient-to-br from-blue-900 to-blue-600 ${isRTL ? 'rtl' : 'ltr'}`}>
      {/* Navigation */}
      <nav className="bg-white shadow-lg">
        <div className="max-w-7xl mx-auto px-4">
          <div className="flex justify-between items-center h-16">
            <div className="flex items-center space-x-4">
              <MessageCircle className="text-blue-600" size={28} />
              <span className="text-xl font-bold text-gray-800">Customer Service</span>
            </div>
            
            <div className="flex items-center space-x-4">
              <button
                onClick={() => setShowLoginPopup(true)}
                className="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 transition-colors"
              >
                {user ? user.username : t.login}
              </button>
              
              <select
                value={currentLanguage}
                onChange={(e) => switchLanguage(e.target.value)}
                className="border border-gray-300 rounded px-3 py-2"
              >
                <option value="en">English</option>
                <option value="es">Español</option>
                <option value="fr">Français</option>
                <option value="ar">العربية</option>
              </select>
            </div>
          </div>
        </div>
      </nav>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-4 py-8">
        <div className="text-center mb-8">
          <h1 className="text-4xl font-bold text-white mb-4">{t.welcome}</h1>
          <p className="text-xl text-blue-100 mb-6">{t.support}</p>
          <button
            onClick={() => setCurrentScreen('chat')}
            className="bg-white text-blue-600 px-8 py-3 rounded-lg font-medium hover:bg-gray-100 transition-colors inline-flex items-center gap-2"
          >
            <MessageCircle size={20} />
            {t.startChat}
          </button>
        </div>

        {/* Menu Grid */}
        <div className="grid grid-cols-2 md:grid-cols-3 gap-6 max-w-4xl mx-auto">
          {[
            { icon: Home, label: t.home, action: () => setCurrentScreen('home') },
            { icon: MessageCircle, label: t.aiChat, action: () => setCurrentScreen('chat') },
            { icon: Calendar, label: t.bookServices, action: () => setCurrentScreen('booking') },
            { icon: MapPin, label: t.trackDelivery, action: () => setCurrentScreen('tracking') },
            { icon: User, label: t.profile, action: () => setCurrentScreen('profile') },
            { icon: FileText, label: t.projectPlanning, action: () => setCurrentScreen('planning') }
          ].map((item, index) => (
            <button
              key={index}
              onClick={item.action}
              className="bg-white/90 backdrop-blur-sm p-6 rounded-xl hover:bg-white transition-all duration-200 transform hover:scale-105 shadow-lg"
            >
              <item.icon className="text-blue-600 mx-auto mb-3" size={32} />
              <span className="text-gray-800 font-medium">{item.label}</span>
            </button>
          ))}
        </div>
      </main>

      {/* Popups */}
      {showLoginPopup && <LoginPopup />}
      {showBookingConfirmation && <BookingConfirmationPopup />}
      {showTrackingResult && <TrackingResultPopup />}
    </div>
  );
};

export default App;