// Internationalization utility for multi-language support
class I18n {
  constructor() {
    this.currentLanguage = 'en';
    this.translations = {};
    this.rtlLanguages = ['ar'];
    this.supportedLanguages = ['en', 'es', 'fr', 'ar'];
    this.fallbackLanguage = 'en';
    
    // Load default translations
    this.loadTranslations();
  }

  loadTranslations() {
    // Mock translations - in real app would load from files
    this.translations = {
      en: {
        welcome: 'Welcome to Customer Service',
        chat: 'AI Chat',
        booking: 'Book Services',
        tracking: 'Track Delivery',
        profile: 'Profile',
        planning: 'Project Planning',
        home: 'Home',
        login: 'Login',
        logout: 'Logout',
        send: 'Send',
        cancel: 'Cancel',
        submit: 'Submit',
        save: 'Save',
        loading: 'Loading...',
        error: 'Error',
        success: 'Success',
        username: 'Username',
        password: 'Password',
        email: 'Email',
        fullName: 'Full Name',
        serviceType: 'Service Type',
        date: 'Date',
        time: 'Time',
        trackingId: 'Tracking ID',
        projectName: 'Project Name',
        requirements: 'Requirements',
        voiceMode: 'Voice Mode',
        switchLanguage: 'Switch Language',
        bookingConfirmed: 'Booking confirmed! Details sent to your email.',
        deliveryStatus: 'Delivery Status',
        typeMessage: 'Type your message...',
        selectDate: 'Select date',
        selectTime: 'Select time',
        enterTrackingId: 'Enter tracking ID',
        enterProjectName: 'Enter project name',
        describeRequirements: 'Describe project requirements',
        supportAtFingerTips: 'Support at Your Fingertips',
        startAiChat: 'Start AI Chat'
      },
      es: {
        welcome: 'Bienvenido al Servicio al Cliente',
        chat: 'Chat IA',
        booking: 'Reservar Servicios',
        tracking: 'Seguir Entrega',
        profile: 'Perfil',
        planning: 'Planificación de Proyectos',
        home: 'Inicio',
        login: 'Iniciar Sesión',
        logout: 'Cerrar Sesión',
        send: 'Enviar',
        cancel: 'Cancelar',
        submit: 'Enviar',
        save: 'Guardar',
        loading: 'Cargando...',
        error: 'Error',
        success: 'Éxito',
        username: 'Nombre de Usuario',
        password: 'Contraseña',
        email: 'Correo Electrónico',
        fullName: 'Nombre Completo',
        serviceType: 'Tipo de Servicio',
        date: 'Fecha',
        time: 'Hora',
        trackingId: 'ID de Seguimiento',
        projectName: 'Nombre del Proyecto',
        requirements: 'Requisitos',
        voiceMode: 'Modo de Voz',
        switchLanguage: 'Cambiar Idioma',
        bookingConfirmed: '¡Reserva confirmada! Detalles enviados a tu correo.',
        deliveryStatus: 'Estado de Entrega',
        typeMessage: 'Escribe tu mensaje...',
        selectDate: 'Seleccionar fecha',
        selectTime: 'Seleccionar hora',
        enterTrackingId: 'Ingresar ID de seguimiento',
        enterProjectName: 'Ingresar nombre del proyecto',
        describeRequirements: 'Describir requisitos del proyecto',
        supportAtFingerTips: 'Soporte al Alcance de tus Dedos',
        startAiChat: 'Iniciar Chat IA'
      },
      fr: {
        welcome: 'Bienvenue au Service Client',
        chat: 'Chat IA',
        booking: 'Réserver Services',
        tracking: 'Suivre Livraison',
        profile: 'Profil',
        planning: 'Planification de Projet',
        home: 'Accueil',
        login: 'Connexion',
        logout: 'Déconnexion',
        send: 'Envoyer',
        cancel: 'Annuler',
        submit: 'Soumettre',
        save: 'Sauvegarder',
        loading: 'Chargement...',
        error: 'Erreur',
        success: 'Succès',
        username: 'Nom d\'utilisateur',
        password: 'Mot de passe',
        email: 'Email',
        fullName: 'Nom Complet',
        serviceType: 'Type de Service',
        date: 'Date',
        time: 'Heure',
        trackingId: 'ID de Suivi',
        projectName: 'Nom du Projet',
        requirements: 'Exigences',
        voiceMode: 'Mode Vocal',
        switchLanguage: 'Changer de Langue',
        bookingConfirmed: 'Réservation confirmée! Détails envoyés à votre email.',
        deliveryStatus: 'Statut de Livraison',
        typeMessage: 'Tapez votre message...',
        selectDate: 'Sélectionner la date',
        selectTime: 'Sélectionner l\'heure',
        enterTrackingId: 'Saisir l\'ID de suivi',
        enterProjectName: 'Saisir le nom du projet',
        describeRequirements: 'Décrire les exigences du projet',
        supportAtFingerTips: 'Support à Portée de Main',
        startAiChat: 'Démarrer Chat IA'
      },
      ar: {
        welcome: 'مرحبا بك في خدمة العملاء',
        chat: 'الدردشة الذكية',
        booking: 'حجز الخدمات',
        tracking: 'تتبع التسليم',
        profile: 'الملف الشخصي',
        planning: 'تخطيط المشروع',
        home: 'الرئيسية',
        login: 'تسجيل الدخول',
        logout: 'تسجيل الخروج',
        send: 'إرسال',
        cancel: 'إلغاء',
        submit: 'إرسال',
        save: 'حفظ',
        loading: 'جاري التحميل...',
        error: 'خطأ',
        success: 'نجح',
        username: 'اسم المستخدم',
        password: 'كلمة المرور',
        email: 'البريد الإلكتروني',
        fullName: 'الاسم الكامل',
        serviceType: 'نوع الخدمة',
        date: 'التاريخ',
        time: 'الوقت',
        trackingId: 'رقم التتبع',
        projectName: 'اسم المشروع',
        requirements: 'المتطلبات',
        voiceMode: 'الوضع الصوتي',
        switchLanguage: 'تغيير اللغة',
        bookingConfirmed: 'تم تأكيد الحجز! تم إرسال التفاصيل إلى بريدك الإلكتروني.',
        deliveryStatus: 'حالة التسليم',
        typeMessage: 'اكتب رسالتك...',
        selectDate: 'اختر التاريخ',
        selectTime: 'اختر الوقت',
        enterTrackingId: 'أدخل رقم التتبع',
        enterProjectName: 'أدخل اسم المشروع',
        describeRequirements: 'وصف متطلبات المشروع',
        supportAtFingerTips: 'الدعم في متناول يدك',
        startAiChat: 'بدء الدردشة الذكية'
      }
    };
  }

  t(key, params = {}) {
    const translation = this.translations[this.currentLanguage]?.[key] || 
                       this.translations[this.fallbackLanguage]?.[key] || 
                       key;
    
    // Replace parameters in translation
    return Object.keys(params).reduce((str, param) => {
      return str.replace(new RegExp(`{{${param}}}`, 'g'), params[param]);
    }, translation);
  }

  setLanguage(language) {
    if (this.supportedLanguages.includes(language)) {
      this.currentLanguage = language;
      
      // Update document direction for RTL languages
      if (this.isRTL(language)) {
        document.documentElement.dir = 'rtl';
        document.documentElement.lang = language;
      } else {
        document.documentElement.dir = 'ltr';
        document.documentElement.lang = language;
      }
      
      return true;
    }
    return false;
  }

  getCurrentLanguage() {
    return this.currentLanguage;
  }

  isRTL(language = this.currentLanguage) {
    return this.rtlLanguages.includes(language);
  }

  getSupportedLanguages() {
    return this.supportedLanguages;
  }

  getLanguageOptions() {
    return this.supportedLanguages.map(lang => ({
      value: lang,
      label: this.getLanguageName(lang)
    }));
  }

  getLanguageName(language) {
    const names = {
      en: 'English',
      es: 'Español',
      fr: 'Français',
      ar: 'العربية'
    };
    return names[language] || language;
  }

  formatDate(date, options = {}) {
    const defaultOptions = {
      year: 'numeric',
      month: 'long',
      day: 'numeric'
    };
    
    return new Intl.DateTimeFormat(this.currentLanguage, {
      ...defaultOptions,
      ...options
    }).format(new Date(date));
  }

  formatTime(time, options = {}) {
    const defaultOptions = {
      hour: '2-digit',
      minute: '2-digit'
    };
    
    return new Intl.DateTimeFormat(this.currentLanguage, {
      ...defaultOptions,
      ...options
    }).format(new Date(`2000-01-01T${time}`));
  }

  formatNumber(number, options = {}) {
    return new Intl.NumberFormat(this.currentLanguage, options).format(number);
  }
}

// Create singleton instance
export const i18n = new I18n();
