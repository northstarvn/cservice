import i18next from 'i18next';
import HttpMiddlware from 'i18next-http-middleware';

i18next.use(HttpMiddleware).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: {
    en: { translation: { welcome: 'Welcome', chat: 'Chat', track: 'Track' } },
    es: { translation: { welcome: 'Bienvenido', chat: 'Chat', track: 'Seguimiento' } },
    fr: { translation: { welcome: 'Bienvenue', chat: 'Chat', track: 'Suivi' } },
    ar: { translation: { welcome: 'مرحبا', chat: 'الدردشة', track: 'تتبع' } }
  }
});

const loadTranslations = async (language) => {
  try {
    await i18next.changeLanguage(language);
    document.documentElement.setAttribute('dir', language === 'ar' ? 'rtl' : 'ltr');
    return i18next.store.data[language].translation;
  } catch (error) {
    console.error('Translation load error:', error);
    await i18next.changeLanguage('en');
    return null;
  }
};

export { i18next, loadTranslations };