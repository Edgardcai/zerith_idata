from pathlib import Path
import pinocchio as pin
import numpy as np
p=Path(sys._MEIPASS)/'urdf/ZR_H1PRO-1.2.00.H.V4.3_URDF_2025.12.02/urdf/ZR_H1PRO-1.2.00.H.V4.3_URDF_2025.12.02.urdf'
model=pin.buildModelFromUrdf(str(p));data=model.createData()
print('JOINTS',list(model.names),flush=True)
for angle in [0,.073815,-.05,.05]:
 q=pin.neutral(model)
 for name,value in [('daogui_joint',.4),('neck_pitch_joint',angle),('neck_yaw_joint',-.00019)]:
  joint=model.joints[model.getJointId(name)];q[joint.idx_q]=value
 gravity=pin.computeGeneralizedGravity(model,data,q)
 print('HEAD_GRAVITY',angle,[(name,float(gravity[model.joints[model.getJointId(name)].idx_v])) for name in ['neck_yaw_joint','neck_pitch_joint']],flush=True)
print('NO_ZCM',blocked,flush=True)
