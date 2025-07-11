import React, { useState, useContext } from 'react';
import { AppContext } from '../context/AppContext';
import { AuthContext } from '../context/AuthContext';
import LoadingSpinner from '../components/LoadingSpinner';
import TrackingResultPopup from '../components/tracking_result_popup';

const Tracking = () => {
  const { showNotification, isLoading, setIsLoading } = useContext(AppContext);
  const { user } = useContext(AuthContext);
  const [trackingId, setTrackingId] = useState('');
  const [showTrackingResult, setShowTrackingResult] = useState(false);
  const [trackingResult, setTrackingResult] = useState(null);

  const handleTrackDelivery = async (e) => {
    e.preventDefault();
    
    if (!trackingId.trim()) {
      showNotification('Please enter a tracking ID', 'error');
      return;
    }

    if (!user) {
      showNotification('Please log in to track deliveries', 'error');
      return;
    }

    setIsLoading(true);

    try {
      // Simulate API call to track delivery
      await new Promise(resolve => setTimeout(resolve, 1500));
      
      // Mock tracking data
      const mockTrackingData = {
        tracking_id: trackingId,
        status: getRandomStatus(),
        last_updated: new Date().toLocaleDateString('en-US', {
          year: 'numeric',
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit'
        }),
        details: {
          origin: 'New York, NY',
          destination: 'Los Angeles, CA',
          estimated_delivery: '2025-07-12',
          carrier: 'FastShip Express'
        }
      };

      setTrackingResult(mockTrackingData);
      setShowTrackingResult(true);
      showNotification('Tracking information retrieved successfully', 'success');
      
    } catch (error) {
      console.error('Tracking error:', error);
      showNotification('Failed to retrieve tracking information', 'error');
    } finally {
      setIsLoading(false);
    }
  };

  const getRandomStatus = () => {
    const statuses = [
      'Order Confirmed',
      'Processing',
      'Shipped',
      'In Transit',
      'Out for Delivery',
      'Delivered'
    ];
    return statuses[Math.floor(Math.random() * statuses.length)];
  };

  const handleReset = () => {
    setTrackingId('');
    setTrackingResult(null);
    setShowTrackingResult(false);
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100 py-8">
      <div className="container mx-auto px-4">
        <div className="max-w-2xl mx-auto">
          {/* Header */}
          <div className="text-center mb-8">
            <div className="flex items-center justify-center mb-4">
              <div className="bg-blue-600 p-3 rounded-full mr-4">
                <svg className="w-8 h-8 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <h1 className="text-4xl font-bold text-gray-800">Track Delivery</h1>
            </div>
            <p className="text-gray-600 text-lg">
              Enter your tracking ID to get real-time delivery updates
            </p>
          </div>

          {/* Tracking Form */}
          <div className="bg-white rounded-xl shadow-lg p-8 mb-6">
            <form onSubmit={handleTrackDelivery} className="space-y-6">
              <div>
                <label htmlFor="tracking_id" className="block text-sm font-medium text-gray-700 mb-2">
                  Tracking ID
                </label>
                <div className="relative">
                  <input
                    type="text"
                    id="tracking_id"
                    value={trackingId}
                    onChange={(e) => setTrackingId(e.target.value)}
                    placeholder="Enter tracking ID (e.g., TRK123456789)"
                    className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all duration-200"
                    disabled={isLoading}
                  />
                  <div className="absolute inset-y-0 right-0 flex items-center pr-3">
                    <svg className="w-5 h-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                  </div>
                </div>
              </div>

              <div className="flex gap-4">
                <button
                  type="submit"
                  disabled={isLoading || !trackingId.trim()}
                  className="flex-1 bg-blue-600 text-white py-3 px-6 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-200 font-medium flex items-center justify-center"
                >
                  {isLoading ? (
                    <LoadingSpinner size="sm" />
                  ) : (
                    <>
                      <svg className="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                      </svg>
                      Track Package
                    </>
                  )}
                </button>
                
                <button
                  type="button"
                  onClick={handleReset}
                  disabled={isLoading}
                  className="px-6 py-3 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-200 font-medium"
                >
                  Reset
                </button>
              </div>
            </form>
          </div>

          {/* Quick Actions */}
          <div className="bg-white rounded-xl shadow-lg p-6">
            <h3 className="text-lg font-semibold text-gray-800 mb-4">Quick Actions</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <button className="p-4 border border-gray-200 rounded-lg hover:bg-gray-50 transition-colors duration-200 text-left">
                <div className="flex items-center">
                  <div className="bg-green-100 p-2 rounded-full mr-3">
                    <svg className="w-5 h-5 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                  </div>
                  <div>
                    <p className="font-medium text-gray-800">Track Multiple</p>
                    <p className="text-sm text-gray-600">Track multiple packages</p>
                  </div>
                </div>
              </button>

              <button className="p-4 border border-gray-200 rounded-lg hover:bg-gray-50 transition-colors duration-200 text-left">
                <div className="flex items-center">
                  <div className="bg-blue-100 p-2 rounded-full mr-3">
                    <svg className="w-5 h-5 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-5 5v-5zM21 2H3a2 2 0 00-2 2v12a2 2 0 002 2h5v5l5-5h8a2 2 0 002-2V4a2 2 0 00-2-2z" />
                    </svg>
                  </div>
                  <div>
                    <p className="font-medium text-gray-800">Get Support</p>
                    <p className="text-sm text-gray-600">Contact customer service</p>
                  </div>
                </div>
              </button>
            </div>
          </div>

          {/* Tracking Tips */}
          <div className="mt-8 bg-blue-50 rounded-xl p-6">
            <h3 className="text-lg font-semibold text-blue-800 mb-4">Tracking Tips</h3>
            <ul className="space-y-2 text-blue-700">
              <li className="flex items-start">
                <span className="text-blue-500 mr-2">•</span>
                Tracking IDs are usually 8-15 characters long
              </li>
              <li className="flex items-start">
                <span className="text-blue-500 mr-2">•</span>
                Updates may take 24-48 hours to reflect
              </li>
              <li className="flex items-start">
                <span className="text-blue-500 mr-2">•</span>
                Contact us if your package seems delayed
              </li>
            </ul>
          </div>
        </div>
      </div>

      {/* Tracking Result Popup */}
      {showTrackingResult && (
        <TrackingResultPopup
          trackingData={trackingResult}
          onClose={() => setShowTrackingResult(false)}
        />
      )}
    </div>
  );
};

export default Tracking;