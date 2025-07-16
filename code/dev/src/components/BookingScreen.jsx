import React from 'react';

const BookingScreen = () => {
  return (
    <div className="p-6">
      <h2>Book Services or Meetings</h2>
      <select><option>Consulting</option><option>Delivery</option><option>Meeting</option></select>
      <input type="date" />
      <input type="time" />
      <button onClick={() => alert('Booking submitted')}>Submit</button>
      <button onClick={() => alert('Cancelled')}>Cancel</button>
    </div>
  );
};

export default BookingScreen;