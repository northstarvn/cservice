import React from 'react';
import { X, Package, MapPin, Clock, CheckCircle, AlertCircle, Truck } from 'lucide-react';
import { useApp } from '../context/app_context';

const TrackingResultPopup = () => {
  const { trackingResult, closeTrackingResult } = useApp();

  if (!trackingResult) return null;

  const getStatusIcon = (status) => {
    switch (status?.toLowerCase()) {
      case 'delivered':
        return <CheckCircle className="w-5 h-5 text-green-500" />;
      case 'in transit':
      case 'out for delivery':
        return <Truck className="w-5 h-5 text-blue-500" />;
      case 'processing':
        return <Package className="w-5 h-5 text-yellow-500" />;
      case 'delayed':
        return <AlertCircle className="w-5 h-5 text-red-500" />;
      default:
        return <Package className="w-5 h-5 text-gray-500" />;
    }
  };

  const getStatusColor = (status) => {
    switch (status?.toLowerCase()) {
      case 'delivered':
        return 'text-green-600 bg-green-50';
      case 'in transit':
      case 'out for delivery':
        return 'text-blue-600 bg-blue-50';
      case 'processing':
        return 'text-yellow-600 bg-yellow-50';
      case 'delayed':
        return 'text-red-600 bg-red-50';
      default:
        return 'text-gray-600 bg-gray-50';
    }
  };

  const formatDate = (dateString) => {
    if (!dateString) return 'N/A';
    return new Date(dateString).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    });
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-lg max-w-md w-full mx-4 shadow-xl">
        <div className="flex items-center justify-between p-4 border-b">
          <h2 className="text-xl font-semibold text-gray-800">
            Delivery Status
          </h2>
          <button
            onClick={closeTrackingResult}
            className="text-gray-500 hover:text-gray-700 transition-colors"
          >
            <X className="w-6 h-6" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {/* Tracking ID */}
          <div className="bg-gray-50 rounded-lg p-3">
            <div className="text-sm text-gray-600">Tracking ID</div>
            <div className="font-medium text-gray-800">{trackingResult.trackingId}</div>
          </div>

          {/* Status */}
          <div className="flex items-center space-x-3">
            {getStatusIcon(trackingResult.status)}
            <div className="flex-1">
              <div className={`inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${getStatusColor(trackingResult.status)}`}>
                {trackingResult.status}
              </div>
            </div>
          </div>

          {/* Details */}
          {trackingResult.details && (
            <div className="space-y-3">
              {trackingResult.details.estimatedDelivery && (
                <div className="flex items-center space-x-2 text-sm">
                  <Clock className="w-4 h-4 text-gray-500" />
                  <span className="text-gray-600">Estimated Delivery:</span>
                  <span className="font-medium">{formatDate(trackingResult.details.estimatedDelivery)}</span>
                </div>
              )}

              {trackingResult.details.currentLocation && (
                <div className="flex items-center space-x-2 text-sm">
                  <MapPin className="w-4 h-4 text-gray-500" />
                  <span className="text-gray-600">Current Location:</span>
                  <span className="font-medium">{trackingResult.details.currentLocation}</span>
                </div>
              )}

              {trackingResult.details.carrier && (
                <div className="flex items-center space-x-2 text-sm">
                  <Truck className="w-4 h-4 text-gray-500" />
                  <span className="text-gray-600">Carrier:</span>
                  <span className="font-medium">{trackingResult.details.carrier}</span>
                </div>
              )}

              {trackingResult.details.trackingHistory && trackingResult.details.trackingHistory.length > 0 && (
                <div className="mt-4">
                  <h4 className="font-medium text-gray-800 mb-2">Tracking History</h4>
                  <div className="space-y-2 max-h-32 overflow-y-auto">
                    {trackingResult.details.trackingHistory.map((event, index) => (
                      <div key={index} className="flex items-start space-x-2 text-sm">
                        <div className="w-2 h-2 bg-blue-500 rounded-full mt-1.5 flex-shrink-0" />
                        <div className="flex-1">
                          <div className="font-medium">{event.status}</div>
                          <div className="text-gray-600">{event.location}</div>
                          <div className="text-gray-500 text-xs">{formatDate(event.timestamp)}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Last Updated */}
          <div className="text-xs text-gray-500 pt-2 border-t">
            Last updated: {formatDate(trackingResult.lastUpdated)}
          </div>
        </div>

        <div className="p-4 border-t bg-gray-50">
          <button
            onClick={closeTrackingResult}
            className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors font-medium"
          >
            OK
          </button>
        </div>
      </div>
    </div>
  );
};

export default TrackingResultPopup;