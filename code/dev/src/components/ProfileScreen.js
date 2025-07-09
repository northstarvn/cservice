import React from 'react';

const ProfileScreen = () => {
  return (
    <div className="p-6">
      <h2>User Profile</h2>
      <input type="text" placeholder="Full Name" />
      <input type="email" placeholder="Email" />
      <select><option>en</option><option>es</option><option>fr</option><option>ar</option></select>
      <button onClick={() => alert('Profile saved')}>Save</button>
    </div>
  );
};

export default ProfileScreen;