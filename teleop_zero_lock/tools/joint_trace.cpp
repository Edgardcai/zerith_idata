// Read-only trace of actual states and commands; no publisher exists.
#include <zcm/zcm-cpp.hpp>
#include "HeadControl.hpp"
#include "HeadState.hpp"
#include "WaistControl.hpp"
#include "WaistState.hpp"
#include "UpperJointControl.hpp"
#include "UpperJointState.hpp"
#include <chrono>
#include <map>
#include <string>
#include <iostream>
#include <iomanip>
struct Trace {
 using Clock=std::chrono::steady_clock;
 std::map<std::string,Clock::time_point> last;
 template<size_t N> void emit(const std::string& c,const float (&values)[N]) {
  auto now=Clock::now();if(now-last[c]<std::chrono::milliseconds(250))return;last[c]=now;
  double stamp=std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
  std::cout<<std::setprecision(12)<<"{\"timestamp\":"<<stamp<<",\"channel\":\""<<c<<"\",\"position\":[";
  for(size_t i=0;i<N;++i){if(i)std::cout<<",";std::cout<<values[i];}std::cout<<"]}"<<std::endl;
 }
 void head_cmd(const zcm::ReceiveBuffer*,const std::string& c,const HeadControl* m){emit(c,m->position);}
 void head(const zcm::ReceiveBuffer*,const std::string& c,const HeadState* m){emit(c,m->position_actual);}
 void waist_cmd(const zcm::ReceiveBuffer*,const std::string& c,const WaistControl* m){emit(c,m->position);}
 void waist(const zcm::ReceiveBuffer*,const std::string& c,const WaistState* m){emit(c,m->position_actual);}
 void arms_cmd(const zcm::ReceiveBuffer*,const std::string& c,const UpperJointControl* m){emit(c,m->position);}
 void arms(const zcm::ReceiveBuffer*,const std::string& c,const UpperJointState* m){emit(c,m->position_actual);}
};
int main(){zcm::ZCM b("ipcshm");if(!b.good())return 1;Trace t;
 b.subscribe("head_control",&Trace::head_cmd,&t);b.subscribe("head_state",&Trace::head,&t);
 b.subscribe("waist_control",&Trace::waist_cmd,&t);b.subscribe("waist_state",&Trace::waist,&t);
 b.subscribe("upper_joint_control",&Trace::arms_cmd,&t);b.subscribe("upper_joint_state",&Trace::arms,&t);b.run();}
