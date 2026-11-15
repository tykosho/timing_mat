$fn=60;

module rf_antenna(){
    // RF Antenna board with dimensions 120x120x1.5
    // antenna is not perfectly centered, add 3 mm tolerance each side for any opening
    union(){
        cube([120,120,1.5]); // board
        translate([20,20,1.5])cube([80,80,6]); // antenna panel
    }
}


board_width = 33.2;
board_length = 53.3;
board_thickness = 1.6;

module r200_board(tol=0){
    translate([0,0,-tol])
    union(){
        difference(){
            // Board
            cube([board_width+2*tol, board_length+2*tol, board_thickness+2*tol]);
            // board mount hole cutout
            //translate([2.5,2.5,-1])cylinder(h=20, d=3);
        }
        // microUSB port
        translate([11,-20,board_thickness])
        cube([10,25,7]);
    }
}

margin = 3;
tolerance = 0.1;
board_side = 120;
inner_z = 12; //inner box open space height
wall_thickness = 4; // must be more than margin above


mount_d = 15.8 + 0.2; // 15.8 plus 0.2 for tolerance
mount_h = 2*31.8;     // height of the camera mount cutout
////////////

module r200_mount(){
    difference(){
    translate([-margin,-margin,-3])
    cube([board_width+2*margin,board_length+0.25*margin,6]);
    #r200_board(tolerance);
    translate([1,0,0])
    cube([board_width-2,board_length+3,15]);
    
    }
}


////////////////
union(){
    
difference(){
    // Main shell
    translate([-margin, -margin, -margin - inner_z])
    cube([board_side+2*margin, board_side+2*margin, 2*margin + inner_z]);
    
    union(){ // Antenna and board models
        rf_antenna();
        translate([0,60,0])
        rf_antenna();
        translate([60-board_width/2,0,-12])
        r200_board();
    }
    
    // hollow out remaining area
    translate([20,0,-inner_z])
    cube([board_side-40,board_side+10,inner_z]);
    
    // stand_mount
    translate([-mount_h/2,(board_side+2*margin)/2,-6])
    rotate([0,90,0])
    #cylinder(d = mount_d, 40, center=true);
}

//Add to the edge for mount

// R200 board mount
translate([60-board_width/2,0,-12])
    r200_mount();        
}

difference(){
    translate([-mount_h/2-margin,-margin,-margin-inner_z])
    #cube([mount_h/2,board_side+2*margin, 2*margin + inner_z]);
    // stand_mount
    translate([-mount_h/2,(board_side+2*margin)/2,-6])
    rotate([0,90,0])
    #cylinder(d = mount_d, 40, center=true);
}
//translate([-100,0,0])r200_mount();
