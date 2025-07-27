import React from 'react';

export const BookingSkeleton = () => (
  <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border p-6 animate-pulse">
    <div className="flex justify-between items-start mb-4">
      <div className="flex items-center gap-3">
        <div className="h-6 bg-gray-200 dark:bg-gray-700 rounded w-32"></div>
        <div className="h-6 bg-gray-200 dark:bg-gray-700 rounded w-20"></div>
      </div>
      <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-24"></div>
    </div>
    
    <div className="grid md:grid-cols-2 gap-4 mb-4">
      <div>
        <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-20 mb-2"></div>
        <div className="h-5 bg-gray-200 dark:bg-gray-700 rounded w-48"></div>
      </div>
      <div>
        <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-16 mb-2"></div>
        <div className="h-5 bg-gray-200 dark:bg-gray-700 rounded w-32"></div>
      </div>
    </div>
    
    <div className="flex gap-2">
      <div className="h-8 bg-gray-200 dark:bg-gray-700 rounded w-32"></div>
      <div className="h-8 bg-gray-200 dark:bg-gray-700 rounded w-20"></div>
    </div>
  </div>
);

export const BookingListSkeleton = ({ count = 3 }) => (
  <div className="grid gap-6">
    {Array.from({ length: count }).map((_, index) => (
      <BookingSkeleton key={index} />
    ))}
  </div>
);