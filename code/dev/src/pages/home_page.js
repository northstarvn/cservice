import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../context/auth_context';
import { useApp } from '../context/app_context';
import { 
  MessageCircle, 
  Calendar, 
  Package, 
  PlaneTakeoff,
  Star,
  ArrowRight,
  Zap,
  Shield,
  Clock,
  Users
} from 'lucide-react';

const Home = () => {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { translations, language } = useApp();

  const features = [
    {
      icon: MessageCircle,
      title: translations?.ai_chat || 'AI Chat',
      description: translations?.ai_chat_desc || 'Get instant help with our AI-powered assistant',
      route: '/chat',
      color: 'bg-blue-500'
    },
    {
      icon: Calendar,
      title: translations?.book_services || 'Book Services',
      description: translations?.book_services_desc || 'Schedule meetings and book services easily',
      route: '/booking',
      color: 'bg-green-500'
    },
    {
      icon: Package,
      title: translations?.track_delivery || 'Track Delivery',
      description: translations?.track_delivery_desc || 'Monitor your delivery status in real-time',
      route: '/tracking',
      color: 'bg-orange-500'
    },
    {
      icon: PlaneTakeoff,
      title: translations?.project_planning || 'Project Planning',
      description: translations?.project_planning_desc || 'AI-assisted project planning and requirements',
      route: '/planning',
      color: 'bg-purple-500'
    }
  ];

  const stats = [
    {
      icon: Users,
      value: '10,000+',
      label: translations?.happy_customers || 'Happy Customers'
    },
    {
      icon: Clock,
      value: '24/7',
      label: translations?.support_available || 'Support Available'
    },
    {
      icon: Zap,
      value: '<2min',
      label: translations?.response_time || 'Response Time'
    },
    {
      icon: Shield,
      value: '99.9%',
      label: translations?.uptime || 'Uptime'
    }
  ];

  const handleFeatureClick = (route) => {
    navigate(route);
  };

  const handleGetStarted = () => {
    if (user) {
      navigate('/chat');
    } else {
      navigate('/login');
    }
  };

  return (
    <div className={`min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100 ${language === 'ar' ? 'rtl' : 'ltr'}`}>
      {/* Hero Section */}
      <div className="relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-r from-blue-600 to-purple-600 opacity-90"></div>
        <div className="relative max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-24">
          <div className="text-center">
            <h1 className="text-4xl md:text-6xl font-bold text-white mb-6">
              {translations?.welcome_title || 'Support at Your Fingertips'}
            </h1>
            <p className="text-xl text-blue-100 mb-8 max-w-3xl mx-auto">
              {translations?.welcome_subtitle || 'Experience next-generation customer service with AI-powered assistance, seamless booking, and real-time tracking.'}
            </p>
            <div className="flex flex-col sm:flex-row gap-4 justify-center">
              <button
                onClick={handleGetStarted}
                className="px-8 py-4 bg-white text-blue-600 rounded-lg font-semibold hover:bg-blue-50 transition-colors duration-200 flex items-center justify-center space-x-2"
              >
                <MessageCircle size={20} />
                <span>{translations?.start_ai_chat || 'Start AI Chat'}</span>
                <ArrowRight size={20} />
              </button>
              <button
                onClick={() => navigate('/booking')}
                className="px-8 py-4 bg-transparent border-2 border-white text-white rounded-lg font-semibold hover:bg-white hover:text-blue-600 transition-colors duration-200"
              >
                {translations?.book_service || 'Book Service'}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Stats Section */}
      <div className="bg-white py-16">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-8">
            {stats.map((stat, index) => {
              const Icon = stat.icon;
              return (
                <div key={index} className="text-center">
                  <div className="flex justify-center mb-4">
                    <div className="p-3 bg-blue-100 rounded-full">
                      <Icon className="w-6 h-6 text-blue-600" />
                    </div>
                  </div>
                  <div className="text-2xl font-bold text-gray-900 mb-2">{stat.value}</div>
                  <div className="text-gray-600">{stat.label}</div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* Features Section */}
      <div className="py-16">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-16">
            <h2 className="text-3xl font-bold text-gray-900 mb-4">
              {translations?.our_services || 'Our Services'}
            </h2>
            <p className="text-xl text-gray-600 max-w-2xl mx-auto">
              {translations?.services_subtitle || 'Discover our comprehensive range of customer service solutions designed to meet your needs.'}
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-8">
            {features.map((feature, index) => {
              const Icon = feature.icon;
              return (
                <div
                  key={index}
                  onClick={() => handleFeatureClick(feature.route)}
                  className="bg-white rounded-xl shadow-lg hover:shadow-xl transition-shadow duration-300 cursor-pointer group"
                >
                  <div className="p-8">
                    <div className="flex justify-center mb-6">
                      <div className={`p-4 ${feature.color} rounded-full group-hover:scale-110 transition-transform duration-200`}>
                        <Icon className="w-8 h-8 text-white" />
                      </div>
                    </div>
                    <h3 className="text-xl font-semibold text-gray-900 mb-3 text-center">
                      {feature.title}
                    </h3>
                    <p className="text-gray-600 text-center mb-4">
                      {feature.description}
                    </p>
                    <div className="flex justify-center">
                      <div className="flex items-center text-blue-600 group-hover:text-blue-800 transition-colors duration-200">
                        <span className="mr-2">{translations?.learn_more || 'Learn More'}</span>
                        <ArrowRight size={16} />
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* User Welcome Section */}
      {user && (
        <div className="bg-gradient-to-r from-green-400 to-blue-500 py-16">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center">
            <h2 className="text-3xl font-bold text-white mb-4">
              {translations?.welcome_back || 'Welcome Back'}, {user.name}!
            </h2>
            <p className="text-xl text-green-100 mb-8">
              {translations?.personalized_experience || 'Continue your personalized customer service experience.'}
            </p>
            <div className="flex flex-col sm:flex-row gap-4 justify-center">
              <button
                onClick={() => navigate('/profile')}
                className="px-6 py-3 bg-white text-green-600 rounded-lg font-semibold hover:bg-green-50 transition-colors duration-200"
              >
                {translations?.view_profile || 'View Profile'}
              </button>
              <button
                onClick={() => navigate('/chat')}
                className="px-6 py-3 bg-transparent border-2 border-white text-white rounded-lg font-semibold hover:bg-white hover:text-green-600 transition-colors duration-200"
              >
                {translations?.continue_chat || 'Continue Chat'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* CTA Section */}
      <div className="bg-gray-900 py-16">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center">
          <h2 className="text-3xl font-bold text-white mb-4">
            {translations?.ready_to_start || 'Ready to Get Started?'}
          </h2>
          <p className="text-xl text-gray-300 mb-8">
            {translations?.cta_subtitle || 'Join thousands of satisfied customers who trust our service.'}
          </p>
          <div className="flex justify-center">
            <button
              onClick={handleGetStarted}
              className="px-8 py-4 bg-blue-600 text-white rounded-lg font-semibold hover:bg-blue-700 transition-colors duration-200 flex items-center space-x-2"
            >
              <Star size={20} />
              <span>{translations?.get_started_now || 'Get Started Now'}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default Home;