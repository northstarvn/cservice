import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

const resources = {
  en: {
    translation: {
      // Navigation
      home: 'Home',
      chat: 'AI Chat',
      booking: 'Book Services',
      tracking: 'Track Delivery',
      profile: 'Profile',
      planning: 'Project Planning',
      login: 'Login',
      logout: 'Logout',
      
      // Common
      welcome: 'Welcome',
      loading: 'Loading...',
      submit: 'Submit',
      cancel: 'Cancel',
      save: 'Save',
      close: 'Close',
      send: 'Send',
      
      // Home
      welcomeTitle: 'Welcome to Customer Service',
      welcomeSubtitle: 'Support at Your Fingertips',
      startChat: 'Start AI Chat',
      
      // Chat
      chatTitle: 'AI Assistant',
      chatPlaceholder: 'Type your message...',
      switchLanguage: 'Switch Language',
      voiceMode: 'Voice Mode',
      
      // Booking
      bookingTitle: 'Book Services or Meetings',
      serviceType: 'Service Type',
      date: 'Date',
      time: 'Time',
      selectDate: 'Select date',
      selectTime: 'Select time',
      submitBooking: 'Submit Booking',
      bookingConfirmed: 'Booking confirmed! Details sent to your email.',
      
      // Tracking
      trackingTitle: 'Track Delivery',
      trackingId: 'Tracking ID',
      trackingPlaceholder: 'Enter tracking ID',
      track: 'Track',
      deliveryStatus: 'Delivery Status',
      
      // Profile
      profileTitle: 'User Profile',
      fullName: 'Full Name',
      email: 'Email',
      preferredLanguage: 'Preferred Language',
      saveProfile: 'Save Profile',
      
      // Planning
      planningTitle: 'Project Planning',
      projectName: 'Project Name',
      requirements: 'Requirements',
      projectRequirements: 'Describe project requirements',
      submitPlan: 'Submit Plan',
      aiSuggest: 'AI Suggest',
      
      // Service Types
      consulting: 'Consulting',
      delivery: 'Delivery',
      meeting: 'Meeting',
      
      // Languages
      english: 'English',
      spanish: 'Spanish',
      french: 'French',
      arabic: 'Arabic',
      
      // Messages
      loginRequired: 'Please log in to access this feature',
      invalidCredentials: 'Invalid username or password',
      bookingSuccess: 'Booking submitted successfully',
      profileSaved: 'Profile saved successfully',
      errorOccurred: 'An error occurred. Please try again.',
      
      // Placeholders
      enterFullName: 'Enter full name',
      enterEmail: 'Enter email',
      enterProjectName: 'Enter project name',
      enterUsername: 'Enter username',
      enterPassword: 'Enter password'
    }
  },
  es: {
    translation: {
      home: 'Inicio',
      chat: 'Chat de IA',
      booking: 'Reservar Servicios',
      tracking: 'Rastrear Entrega',
      profile: 'Perfil',
      planning: 'Planificación de Proyectos',
      login: 'Iniciar Sesión',
      logout: 'Cerrar Sesión',
      
      welcome: 'Bienvenido',
      loading: 'Cargando...',
      submit: 'Enviar',
      cancel: 'Cancelar',
      save: 'Guardar',
      close: 'Cerrar',
      send: 'Enviar',
      
      welcomeTitle: 'Bienvenido al Servicio al Cliente',
      welcomeSubtitle: 'Soporte al Alcance de Tus Dedos',
      startChat: 'Iniciar Chat de IA',
      
      chatTitle: 'Asistente de IA',
      chatPlaceholder: 'Escribe tu mensaje...',
      switchLanguage: 'Cambiar Idioma',
      voiceMode: 'Modo de Voz',
      
      bookingTitle: 'Reservar Servicios o Reuniones',
      serviceType: 'Tipo de Servicio',
      date: 'Fecha',
      time: 'Hora',
      selectDate: 'Seleccionar fecha',
      selectTime: 'Seleccionar hora',
      submitBooking: 'Enviar Reserva',
      bookingConfirmed: '¡Reserva confirmada! Detalles enviados a tu correo.',
      
      trackingTitle: 'Rastrear Entrega',
      trackingId: 'ID de Rastreo',
      trackingPlaceholder: 'Ingresa el ID de rastreo',
      track: 'Rastrear',
      deliveryStatus: 'Estado de Entrega',
      
      profileTitle: 'Perfil de Usuario',
      fullName: 'Nombre Completo',
      email: 'Correo Electrónico',
      preferredLanguage: 'Idioma Preferido',
      saveProfile: 'Guardar Perfil',
      
      planningTitle: 'Planificación de Proyectos',
      projectName: 'Nombre del Proyecto',
      requirements: 'Requisitos',
      projectRequirements: 'Describe los requisitos del proyecto',
      submitPlan: 'Enviar Plan',
      aiSuggest: 'Sugerir con IA',
      
      consulting: 'Consultoría',
      delivery: 'Entrega',
      meeting: 'Reunión',
      
      english: 'Inglés',
      spanish: 'Español',
      french: 'Francés',
      arabic: 'Árabe'
    }
  },
  fr: {
    translation: {
      home: 'Accueil',
      chat: 'Chat IA',
      booking: 'Réserver Services',
      tracking: 'Suivre Livraison',
      profile: 'Profil',
      planning: 'Planification de Projet',
      login: 'Connexion',
      logout: 'Déconnexion',
      
      welcome: 'Bienvenue',
      loading: 'Chargement...',
      submit: 'Soumettre',
      cancel: 'Annuler',
      save: 'Sauvegarder',
      close: 'Fermer',
      send: 'Envoyer',
      
      welcomeTitle: 'Bienvenue au Service Client',
      welcomeSubtitle: 'Support à Portée de Main',
      startChat: 'Démarrer Chat IA',
      
      chatTitle: 'Assistant IA',
      chatPlaceholder: 'Tapez votre message...',
      switchLanguage: 'Changer de Langue',
      voiceMode: 'Mode Vocal',
      
      bookingTitle: 'Réserver Services ou Réunions',
      serviceType: 'Type de Service',
      date: 'Date',
      time: 'Heure',
      selectDate: 'Sélectionner date',
      selectTime: 'Sélectionner heure',
      submitBooking: 'Soumettre Réservation',
      bookingConfirmed: 'Réservation confirmée! Détails envoyés à votre email.',
      
      trackingTitle: 'Suivre Livraison',
      trackingId: 'ID de Suivi',
      trackingPlaceholder: 'Entrez l\'ID de suivi',
      track: 'Suivre',
      deliveryStatus: 'Statut de Livraison',
      
      profileTitle: 'Profil Utilisateur',
      fullName: 'Nom Complet',
      email: 'Email',
      preferredLanguage: 'Langue Préférée',
      saveProfile: 'Sauvegarder Profil',
      
      planningTitle: 'Planification de Projet',
      projectName: 'Nom du Projet',
      requirements: 'Exigences',
      projectRequirements: 'Décrire les exigences du projet',
      submitPlan: 'Soumettre Plan',
      aiSuggest: 'Suggérer avec IA',
      
      consulting: 'Conseil',
      delivery: 'Livraison',
      meeting: 'Réunion',
      
      english: 'Anglais',
      spanish: 'Espagnol',
      french: 'Français',
      arabic: 'Arabe'
    }
  },
  ar: {
    translation: {
      home: 'الرئيسية',
      chat: 'الدردشة بالذكاء الاصطناعي',
      booking: 'حجز الخدمات',
      tracking: 'تتبع التسليم',
      profile: 'الملف الشخصي',
      planning: 'تخطيط المشروع',
      login: 'تسجيل الدخول',
      logout: 'تسجيل الخروج',
      
      welcome: 'مرحبا',
      loading: 'جاري التحميل...',
      submit: 'إرسال',
      cancel: 'إلغاء',
      save: 'حفظ',
      close: 'إغلاق',
      send: 'إرسال',
      
      welcomeTitle: 'مرحبا بكم في خدمة العملاء',
      welcomeSubtitle: 'الدعم في متناول يديك',
      startChat: 'بدء الدردشة بالذكاء الاصطناعي',
      
      chatTitle: 'مساعد الذكاء الاصطناعي',
      chatPlaceholder: 'اكتب رسالتك...',
      switchLanguage: 'تغيير اللغة',
      voiceMode: 'وضع الصوت',
      
      bookingTitle: 'حجز الخدمات أو الاجتماعات',
      serviceType: 'نوع الخدمة',
      date: 'التاريخ',
      time: 'الوقت',
      selectDate: 'اختر التاريخ',
      selectTime: 'اختر الوقت',
      submitBooking: 'إرسال الحجز',
      bookingConfirmed: 'تم تأكيد الحجز! تم إرسال التفاصيل إلى بريدك الإلكتروني.',
      
      trackingTitle: 'تتبع التسليم',
      trackingId: 'رقم التتبع',
      trackingPlaceholder: 'أدخل رقم التتبع',
      track: 'تتبع',
      deliveryStatus: 'حالة التسليم',
      
      profileTitle: 'الملف الشخصي للمستخدم',
      fullName: 'الاسم الكامل',
      email: 'البريد الإلكتروني',
      preferredLanguage: 'اللغة المفضلة',
      saveProfile: 'حفظ الملف الشخصي',
      
      planningTitle: 'تخطيط المشروع',
      projectName: 'اسم المشروع',
      requirements: 'المتطلبات',
      projectRequirements: 'وصف متطلبات المشروع',
      submitPlan: 'إرسال الخطة',
      aiSuggest: 'اقتراح بالذكاء الاصطناعي',
      
      consulting: 'الاستشارة',
      delivery: 'التسليم',
      meeting: 'الاجتماع',
      
      english: 'الإنجليزية',
      spanish: 'الإسبانية',
      french: 'الفرنسية',
      arabic: 'العربية'
    }
  }
};

i18n
  .use(initReactI18next)
  .init({
    resources,
    lng: 'en',
    fallbackLng: 'en',
    interpolation: {
      escapeValue: false
    }
  });

export default i18n;