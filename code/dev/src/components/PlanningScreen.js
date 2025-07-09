import React from 'react';

const PlanningScreen = () => {
  return (
    <div className="p-6">
      <h2>Project Planning</h2>
      <input type="text" placeholder="Project Name" />
      <textarea placeholder="Requirements"></textarea>
      <button onClick={() => alert('AI suggestions generated')}>AI Suggest</button>
      <button onClick={() => alert('Plan submitted')}>Submit</button>
    </div>
  );
};

export default PlanningScreen;